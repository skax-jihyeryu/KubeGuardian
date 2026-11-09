from datetime import UTC, datetime, timedelta

from sqlalchemy import event
from sqlalchemy.orm import Session

from api.viewmodels.check_list import CheckFilters, list_checks
from core.db.tables import CheckRunRow
from tests.api.seed import at, finish_check, make_run


def test_list_filters_and_search_persist(client, engine):
    run = make_run(engine)
    finish_check(engine, run, path="B", severity="critical")
    response = client.get("/dev/checks?severity=critical&trigger=schedule&period=7d&q=chat")
    assert response.status_code == 200
    assert 'value="critical" selected' in response.text
    assert 'value="chat"' in response.text
    assert client.get("/dev/checks?cursor=oops").status_code == 404


def test_ten_thousand_runs_page_without_snapshot(engine):
    with Session(engine) as session, session.begin():
        session.add_all(
            CheckRunRow(trigger="schedule", started_at=at(-i * 300)) for i in range(10_000)
        )
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        page = list_checks(engine, CheckFilters(), None, at(21))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(page.rows) == 50
    assert page.next_url
    assert all("snapshot" not in s.lower() for s in statements)
    older = list_checks(engine, CheckFilters(), page.rows[-1].id, at(21))
    assert older.rows[0].id < page.rows[-1].id


def test_normal_runs_remain_individual_latest_first_with_gap(client, engine):
    first = make_run(engine, start=at(-1800))
    finish_check(engine, first, path="C")
    for seconds in (0, 300):
        run = make_run(engine, start=at(seconds))
        finish_check(engine, run, path="C")
    page = list_checks(engine, CheckFilters(), None, at(330))
    assert [row.id for row in page.rows] == [3, 2, 1]
    assert page.rows[1].gap == 30
    import re

    for path in ("/dev/checks", f"/dev/checks/{run}"):
        text = client.get(path).text
        section = re.search(r'<section class="list.*?</section>', text, re.S).group()
        assert "<details" not in section.split("</form>", 1)[1]
        ids = re.findall(r'href="/dev/checks/(\d+)\?period=all"', section)
        assert ids == ["3", "2", "1"]


def test_detail_contains_list(client, engine):
    run = make_run(engine)
    finish_check(engine, run, path="C")
    response = client.get(f"/dev/checks/{run}")
    assert 'aria-label="점검 목록"' in response.text
    assert 'aria-label="점검 상세"' in response.text


def test_final_severity_filter_and_multi_issue_highest(engine):
    from core.db.tables import IncidentRow
    from tests.api.seed import finish_investigation, open_incident, report, start_investigation

    run = make_run(engine)
    finish_check(engine, run, path="B", severity="critical")
    incident = open_incident(engine, run, "critical")
    with Session(engine) as session, session.begin():
        session.get(IncidentRow, incident).final_severity = "latent_risk"
    data = report()
    data.issues[0].impact = "latent_risk"
    inv = start_investigation(engine, run, incident, [])
    finish_investigation(engine, inv, data)
    assert not list_checks(engine, CheckFilters(severity="critical"), None, at(30)).rows
    row = list_checks(engine, CheckFilters(severity="risk"), None, at(30)).rows[0]
    assert row.severity.key == "risk"
    second_incident = open_incident(engine, run, "warning")
    data = report()
    data.issues[0].impact = "partial_users"
    inv = start_investigation(engine, run, second_incident, [])
    finish_investigation(engine, inv, data)
    row = list_checks(engine, CheckFilters(), None, at(30)).rows[0]
    assert row.severity.key == "warn"
    assert row.more_issues == 1


def test_tip_and_calm_filters(engine):
    from service.model import Signal

    run = make_run(engine)
    finish_check(
        engine,
        run,
        path="C",
        signals=[
            Signal(rule_id="H01", verdict="detected", subject="dev/Deployment/a", severity="info")
        ],
    )
    assert (
        list_checks(engine, CheckFilters(severity="tip"), None, at(30)).rows[0].severity.key
        == "tip"
    )
    assert not list_checks(engine, CheckFilters(severity="calm"), None, at(30)).rows
    calm = make_run(engine)
    finish_check(engine, calm, path="C")
    assert [
        r.id for r in list_checks(engine, CheckFilters(severity="calm"), None, at(30)).rows
    ] == [calm]


def test_filtered_out_runs_do_not_create_false_gap(engine):
    for i in range(10):
        run = make_run(engine, start=at(i * 300))
        finish_check(engine, run, path="B", severity="critical" if i in (0, 9) else "warning")
    page = list_checks(engine, CheckFilters(severity="critical"), None, at(2800))
    assert len(page.rows) == 2
    assert all(row.gap == 0 for row in page.rows)


def test_running_filter_uses_last_phase_heartbeat(engine):
    from core.db import CheckResultStore

    run = make_run(engine, start=at(0))
    store = CheckResultStore(engine)
    store.start(run, at(0))
    store.phase(run, "rules", at(0), at(290), "still working")
    assert list_checks(engine, CheckFilters(status="running"), None, at(300)).rows[0].id == run
    assert not list_checks(engine, CheckFilters(status="halted"), None, at(300)).rows
    assert list_checks(engine, CheckFilters(status="halted"), None, at(600)).rows[0].id == run


def test_row_links_and_detail_keep_page_filters(client, engine):
    for _i in range(55):
        run = make_run(engine)
        finish_check(engine, run, path="B", severity="critical")
    page = list_checks(engine, CheckFilters(severity="critical", period="7d"), 6, at(30))
    assert "cursor=6" in page.rows[0].url
    assert "severity=critical" in page.rows[0].url
    response = client.get(page.rows[0].url)
    assert response.status_code == 200
    assert "cursor=6" in response.text
    assert "/checks/55?" not in response.text


def test_resolved_incident_does_not_color_recovered_check(engine):
    from tests.api.seed import link_incident, open_incident

    original = make_run(engine, start=at(0))
    finish_check(engine, original, path="B", severity="critical")
    incident = open_incident(engine, original, "critical")
    recovered = make_run(engine, start=at(300))
    finish_check(engine, recovered, path="C")
    link_incident(engine, incident, recovered, "resolved")
    page = list_checks(engine, CheckFilters(), None, at(330))
    row = page.rows[0]
    assert row.id == recovered
    assert row.severity is None
    assert "해결" in row.relation
    assert row.status == "완료"
    assert [
        r.id for r in list_checks(engine, CheckFilters(severity="calm"), None, at(330)).rows
    ] == [recovered]
    assert [
        r.id for r in list_checks(engine, CheckFilters(severity="critical"), None, at(330)).rows
    ] == [original]


def test_historic_severity_uses_each_runs_report_after_incident_changes(engine):
    from core.db.tables import IncidentRow
    from tests.api.seed import (
        finish_investigation,
        link_incident,
        open_incident,
        report,
        start_investigation,
    )

    first = make_run(engine, start=at(0))
    finish_check(engine, first, path="B", severity="critical")
    incident = open_incident(engine, first, "critical")
    first_report = report()
    first_report.issues[0].impact = "latent_risk"
    inv = start_investigation(engine, first, incident, [])
    finish_investigation(engine, inv, first_report)
    second = make_run(engine, start=at(300))
    finish_check(engine, second, path="B", severity="critical")
    link_incident(engine, incident, second, "changed")
    inv = start_investigation(engine, second, incident, [])
    finish_investigation(engine, inv, report())
    with Session(engine) as session, session.begin():
        session.get(IncidentRow, incident).final_severity = "core_path_broken"
    assert [
        row.id for row in list_checks(engine, CheckFilters(severity="risk"), None, at(400)).rows
    ] == [first]
    assert [
        row.id for row in list_checks(engine, CheckFilters(severity="critical"), None, at(400)).rows
    ] == [second]
    assert list_checks(engine, CheckFilters(), None, at(400)).rows[1].severity.key == "risk"


def test_search_matches_korean_symptom_values_and_report_issue_title(engine):
    from service.model import Symptom
    from tests.api.seed import finish_investigation, open_incident, report, start_investigation

    symptom = make_run(engine)
    finish_check(
        engine,
        symptom,
        path="B",
        severity="critical",
        symptoms=[
            Symptom(
                scenario="chat",
                status="failed",
                summary="응답본문의 한글오류단서",
                severity="critical",
            )
        ],
    )
    reported = make_run(engine)
    finish_check(engine, reported, path="B", severity="critical")
    incident = open_incident(engine, reported)
    data = report()
    data.issues[0].title = "배포후인증실패 제목단서"
    inv = start_investigation(engine, reported, incident, [])
    finish_investigation(engine, inv, data)
    assert [
        row.id for row in list_checks(engine, CheckFilters(q="한글오류단서"), None, at(400)).rows
    ] == [symptom]
    assert [
        row.id for row in list_checks(engine, CheckFilters(q="제목단서"), None, at(400)).rows
    ] == [reported]


def test_search_matches_decoded_korean_rule_reason(engine):
    from service.model import Signal

    run = make_run(engine)
    finish_check(
        engine,
        run,
        path="C",
        signals=[
            Signal(
                rule_id="S01",
                verdict="unknown",
                subject="dev/Service/agent",
                message="서비스판정실패단서",
            )
        ],
    )
    assert [
        row.id
        for row in list_checks(engine, CheckFilters(q="서비스판정실패단서"), None, at(30)).rows
    ] == [run]


def test_latest_report_top_issue_matches_detail_and_missing_report_falls_back(engine):
    from api.viewmodels.checks import build_check_detail
    from tests.api.seed import finish_investigation, open_incident, report, start_investigation

    run = make_run(engine)
    finish_check(engine, run, path="B", severity="critical")
    incident = open_incident(engine, run)
    data = report()
    high = data.issues[0].model_copy(update={"rank": 2, "impact": "core_path_broken"})
    low = data.issues[0].model_copy(update={"rank": 1, "impact": "latent_risk"})
    data.issues = [high, low]
    inv = start_investigation(engine, run, incident, [])
    finish_investigation(engine, inv, data)
    row = list_checks(engine, CheckFilters(), None, at(100)).rows[0]
    assert (
        row.severity.key == build_check_detail(engine, "dev", run, at(100)).header.sev.key == "risk"
    )
    start_investigation(engine, run, incident, [])
    assert list_checks(engine, CheckFilters(), None, at(100)).rows[0].severity.key == "crit"


def test_unknown_advice_does_not_count_or_color_list_as_recommendation(engine):
    from core.db import CheckResultStore
    from service.model import GateResult, Signal

    run = make_run(engine)
    store = CheckResultStore(engine)
    store.start(run, at(0))
    store.finish(
        run,
        "done",
        at(5),
        GateResult(path="C"),
        [
            Signal(
                rule_id="H01",
                verdict="unknown",
                subject="dev/Deployment/agent",
                message="Pod 수집 실패",
            )
        ],
        [],
        rules=["H01"],
    )
    row = list_checks(engine, CheckFilters(), None, at(30)).rows[0]
    assert row.unknown == 1
    assert row.advice == 0
    assert row.severity is None
    assert row.status == "일부 확인하지 못함"
    assert not list_checks(engine, CheckFilters(severity="tip"), None, at(30)).rows


def test_unfinished_check_summary_does_not_claim_normal(engine):
    from core.db import CheckResultStore

    run = make_run(engine)
    store = CheckResultStore(engine)
    store.start(run, at(0))
    assert list_checks(engine, CheckFilters(), None, at(20)).rows[0].summary == "점검 진행 중"
    assert list_checks(engine, CheckFilters(), None, at(300)).rows[0].summary == "점검 중단"
    store.finish(run, "failed", at(30), None, [], [], error="RuntimeError")
    assert list_checks(engine, CheckFilters(), None, at(40)).rows[0].summary == "점검 실패"


def _runs_around_kst_midnight(engine):
    """KST 11/02 23:59, 11/03 00:00, 11/03 23:59, 11/04 00:00."""
    starts = [
        datetime(2026, 11, 2, 14, 59, tzinfo=UTC),
        datetime(2026, 11, 2, 15, 0, tzinfo=UTC),
        datetime(2026, 11, 3, 14, 59, tzinfo=UTC),
        datetime(2026, 11, 3, 15, 0, tzinfo=UTC),
    ]
    return [make_run(engine, start=s) for s in starts]


def test_date_range_uses_kst_days_inclusive(engine):
    ids = _runs_around_kst_midnight(engine)
    now = datetime(2026, 11, 9, tzinfo=UTC)
    page = list_checks(
        engine, CheckFilters.from_query({"from": "2026-11-03", "to": "2026-11-03"}), None, now
    )
    assert [r.id for r in page.rows] == [ids[2], ids[1]]
    only_from = list_checks(engine, CheckFilters.from_query({"from": "2026-11-04"}), None, now)
    assert [r.id for r in only_from.rows] == [ids[3]]
    only_to = list_checks(engine, CheckFilters.from_query({"to": "2026-11-02"}), None, now)
    assert [r.id for r in only_to.rows] == [ids[0]]


def test_date_range_overrides_quick_period(engine):
    ids = _runs_around_kst_midnight(engine)
    now = datetime(2026, 11, 9, tzinfo=UTC)
    filters = CheckFilters.from_query({"period": "1d", "from": "2026-11-03", "to": "2026-11-03"})
    assert [r.id for r in list_checks(engine, filters, None, now).rows] == [ids[2], ids[1]]


def test_invalid_or_reversed_dates_are_ignored(engine):
    ids = _runs_around_kst_midnight(engine)
    now = datetime(2026, 11, 9, tzinfo=UTC)
    for query in (
        {"from": "2026-13-01"},
        {"from": "2026/11/03"},
        {"from": "20261103"},
        {"from": "２０２６-11-03"},
        {"from": "2026-11-03T00:00"},
        {"from": "x' OR 1=1 --"},
        {"from": "2026-11-04", "to": "2026-11-03"},
    ):
        filters = CheckFilters.from_query(query)
        assert filters.date_from == filters.date_to == "", query
        assert len(list_checks(engine, filters, None, now).rows) == len(ids), query


def test_date_range_persists_in_page_and_links(client, engine):
    for i in range(51):
        make_run(engine, start=datetime(2026, 11, 3, 1, 0, tzinfo=UTC) + timedelta(minutes=i))
    response = client.get("/dev/checks?from=2026-11-03&to=2026-11-03")
    assert 'name="from" value="2026-11-03"' in response.text
    assert 'name="to" value="2026-11-03"' in response.text
    assert "from=2026-11-03" in response.text and "to=2026-11-03" in response.text
    next_url = list_checks(
        engine,
        CheckFilters.from_query({"from": "2026-11-03", "to": "2026-11-03"}),
        None,
        datetime(2026, 11, 9, tzinfo=UTC),
    ).next_url
    assert "from=2026-11-03" in next_url and "to=2026-11-03" in next_url


def test_dates_reset_quick_period_filter():
    filters = CheckFilters.from_query({"period": "7d", "from": "2026-11-03"})
    assert filters.period == "all"
    assert "period" not in filters.query() and filters.query()["from"] == "2026-11-03"
    assert CheckFilters.from_query({"period": "7d"}).period == "7d"


def test_checks_page_groups_quick_and_custom_periods(client):
    html = client.get("/dev/checks?period=7d&from=2026-11-03").text
    picker = html.split("data-period-picker", 1)[1].split("</details>", 1)[0]
    assert 'name="period" value="custom"' in picker
    assert 'data-period-quick="7d"' in picker
    assert 'data-period-quick="all"' not in html
    assert 'class="period-chevron"' in picker
    assert html.count('name="period"') == 1
    assert 'name="from" value="2026-11-03"' in picker


def test_checks_page_ignores_extreme_dates(client):
    for query in ("to=9999-12-31", "from=0001-01-01", "from=0001-01-01&to=9999-12-31"):
        response = client.get(f"/dev/checks?{query}")
        assert response.status_code == 200, query
        assert 'name="from" value=""' in response.text and 'name="to" value=""' in response.text
