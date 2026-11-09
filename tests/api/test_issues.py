"""이슈 화면은 저장된 보고서만 읽고 다른 이슈의 보고서를 섞지 않는다."""

import pytest

from core.db import AlertStore, IncidentStore
from tests.api.seed import (
    add_evidence,
    at,
    finish_investigation,
    make_run,
    open_incident,
    report,
    start_investigation,
)


def test_issue_list_and_report(client, engine):
    run = make_run(engine)
    inc = open_incident(engine, run)
    ev = add_evidence(engine, run)
    inv = start_investigation(engine, run, inc, [])
    finish_investigation(engine, inv, report(evidence_ids=(ev,), actions=["kubectl get pods"]))
    AlertStore(engine).add(
        kind="M1",
        channel="C1",
        text="<script>bad</script>",
        sent_at=at(2),
        incident_id=inc,
        dry_run=True,
    )
    response = client.get(f"/dev/issues/{inc}")
    assert response.status_code == 200
    assert "할당량 소진" in response.text
    assert "권장 조치" in response.text
    assert "긴급 · 핵심 경로 장애" in response.text
    assert "dry-run 기록" in response.text
    assert "&lt;script&gt;bad&lt;/script&gt;" in response.text
    assert f"/dev/checks/{run}?ev=1" in response.text
    assert client.get("/dev/issues?status=resolved").text.count('class="irow') == 0
    assert "채팅 실패" in client.get("/dev/issues?q=할당량").text
    IncidentStore(engine).resolve(inc, make_run(engine, start=at(70)), at(70))
    assert "해결 14:06" in client.get(f"/dev/issues/{inc}").text


def test_issue_pending_running_and_failed(client, engine):
    run = make_run(engine)
    inc = open_incident(engine, run)
    assert "조사 대기 중" in client.get(f"/dev/issues/{inc}").text
    inv = start_investigation(engine, run, inc, [])
    assert 'hx-trigger="every 3s"' in client.get(f"/dev/issues/{inc}").text
    finish_investigation(engine, inv, None, status="failed")
    assert "조사 실패" in client.get(f"/dev/issues/{inc}").text


def test_issue_report_scope_and_unknown(client, engine):
    run = make_run(engine)
    inc = open_incident(engine, run)
    other = open_incident(engine, run)
    inv = start_investigation(engine, run, other, [])
    assert client.get(f"/dev/issues/{inc}?report={inv}").status_code == 404
    assert client.get("/dev/issues/999").status_code == 404
    assert client.get("/wrong/issues").status_code == 404
    assert client.get("/dev/issues/nope").status_code == 404


def test_invalid_stored_report_and_previous(client, engine):
    from sqlalchemy.orm import Session

    from core.db.tables import InvestigationRow

    run = make_run(engine)
    inc = open_incident(engine, run)
    first = start_investigation(engine, run, inc, [])
    finish_investigation(engine, first, report())
    second = start_investigation(engine, run, inc, [])
    with Session(engine) as s, s.begin():
        row = s.get_one(InvestigationRow, second)
        row.status, row.report = "done", {"bad": "data"}
    response = client.get(f"/dev/issues/{inc}")
    assert response.status_code == 200
    assert "저장된 보고서를 읽을 수 없습니다" in response.text
    assert f"&report={first}" in response.text


def test_queued_issue_polls_then_stale_running_stops(client, engine, clock):
    from core.db import InvestigationQueue

    run = make_run(engine)
    inc = open_incident(engine, run)
    InvestigationQueue(engine).put(inc, run, {}, at(0))
    response = client.get(f"/dev/issues/{inc}")
    assert "조사 대기 중" in response.text
    assert 'hx-trigger="every 3s"' in response.text
    inv = start_investigation(engine, run, inc, [])
    response = client.get(f"/dev/issues/{inc}?report={inv}")
    assert f'hx-get="http://testserver/dev/issues/{inc}?report={inv}"' in response.text
    clock.now = at(700)
    response = client.get(f"/dev/issues/{inc}")
    assert "조사가 중단되었습니다" in response.text
    assert 'hx-trigger="every 3s"' not in response.text


def test_period_filter_and_action_copy(client, engine, clock):
    clock.now = at(3 * 86400)
    run = make_run(engine)
    inc = open_incident(engine, run)
    inv = start_investigation(engine, run, inc, [])
    finish_investigation(engine, inv, report(actions=["kubectl get pods <script>"]))
    response = client.get("/dev/issues?days=1")
    assert 'aria-label="기간"' in response.text
    assert 'class="irow' not in response.text
    assert 'class="irow' in client.get("/dev/issues?days=7").text
    response = client.get(f"/dev/issues/{inc}")
    assert 'data-copy-action="kubectl get pods &lt;script&gt;"' in response.text


def test_issue_filters_survive_selection(client, engine):
    run = make_run(engine)
    inc = open_incident(engine, run)
    query = "status=open&severity=crit&q=chat&days=7"
    response = client.get("/dev/issues?" + query)
    assert f"/issues/{inc}?status=open&amp;severity=crit&amp;q=chat&amp;days=7" in response.text
    response = client.get(f"/dev/issues/{inc}?" + query)
    assert 'value="open" selected' in response.text
    assert 'value="crit" selected' in response.text
    assert 'value="chat"' in response.text
    assert 'value="7" selected' in response.text


def test_timeline_reminder_splits_continued_group(client, engine, clock):
    from api.viewmodels.issues import build_issue_detail
    from tests.api.seed import link_incident

    run = make_run(engine)
    inc = open_incident(engine, run)
    for index in (1, 2, 3):
        current = make_run(engine, start=at(index * 300))
        link_incident(engine, inc, current, "continued")
    AlertStore(engine).add(
        kind="M2", channel="C", text="reminder", sent_at=at(450), incident_id=inc
    )
    timeline = build_issue_detail(engine, inc, None, clock.now).timeline
    assert [(event.label, event.count) for event in timeline] == [
        ("점검 · 열림", 1),
        ("이어짐 점검", 1),
        ("Slack · 리마인더", 1),
        ("이어짐 점검", 2),
    ]


def test_list_title_is_top_issue_title_but_search_finds_conclusion(client, engine):
    run = make_run(engine)
    inc = open_incident(engine, run)
    inv = start_investigation(engine, run, inc, [])
    finish_investigation(engine, inv, report(conclusion="긴 결론 전문 키워드xyz"))
    text = client.get("/dev/issues").text
    assert '<div class="ttl">채팅 실패</div>' in text
    assert "긴 결론 전문" not in text
    assert "채팅 실패" in client.get("/dev/issues?q=키워드xyz").text


def test_list_title_falls_back_without_issues_or_report(client, engine):
    from api.viewmodels.issues import issue_row
    from core.db import IncidentStore

    run = make_run(engine)
    inc = open_incident(engine, run)
    incident = IncidentStore(engine).get(inc)
    fallback = ", ".join(incident.features or incident.services) or "장애 신호·기능 점검 실패"
    assert issue_row(engine, incident).title == fallback
    inv = start_investigation(engine, run, inc, [])
    finish_investigation(engine, inv, report().model_copy(update={"issues": []}))
    row = issue_row(engine, incident)
    assert row.title == fallback
    assert client.get(f"/dev/issues/{inc}").status_code == 200


def _done_issue(engine, **kw):
    run = make_run(engine)
    inc = open_incident(engine, run)
    inv = start_investigation(engine, run, inc, [])
    finish_investigation(engine, inv, report(**kw))
    return inc


def test_report_sections_in_order_with_numbers(client, engine):
    inc = _done_issue(engine, verification="/kg check 로 확인")
    text = client.get(f"/dev/issues/{inc}").text
    titles = [
        "1. 결론",
        "2. 문제",
        "3. 사전 확인",
        "4. 가설 판정",
        "5. 확인한 범위",
        "6. 권장 조치",
        "7. 재검증",
    ]
    positions = [text.index(t) for t in titles]
    assert positions == sorted(positions)
    assert text.index("영향:") > text.index("1. 결론")
    assert text.index("영향:") < text.index("2. 문제")
    assert text.index("점검 과정 보기") > text.index("7. 재검증")


def test_report_without_verification_has_no_seventh_section(client, engine):
    inc = _done_issue(engine)
    text = client.get(f"/dev/issues/{inc}").text
    assert "6. 권장 조치" in text
    assert "7. 재검증" not in text


def _hyps():
    from service.model.report import Hypothesis

    return [
        Hypothesis(statement="지지 가설", status="supported", reason="r1"),
        Hypothesis(statement="미결 가설", status="inconclusive", reason="r2"),
        Hypothesis(statement="배제 가설", status="refuted", reason="r3"),
    ]


def test_refuted_hypotheses_are_folded(client, engine):
    text = client.get(f"/dev/issues/{_done_issue(engine, hypotheses=_hyps())}").text
    head, _, folded = text.partition("<details><summary>배제한 가설 1건</summary>")
    assert folded
    assert "지지 가설" in head and "미결 가설" in head
    assert "배제 가설" not in head
    assert "배제 가설" in folded.split("</details>")[0]
    assert 'class="nowrap"' in head


def test_no_refuted_means_no_fold_and_only_refuted_means_no_table(client, engine):
    from service.model.report import Hypothesis

    only_supported = [Hypothesis(statement="지지 가설", status="supported")]
    text = client.get(f"/dev/issues/{_done_issue(engine, hypotheses=only_supported)}").text
    assert "배제한 가설" not in text
    only_refuted = [Hypothesis(statement="배제 가설", status="refuted")]
    text = client.get(f"/dev/issues/{_done_issue(engine, hypotheses=only_refuted)}").text
    section = text.split("4. 가설 판정")[1].split("5. 확인한 범위")[0]
    assert "<table" not in section.split("<details>")[0]
    assert "배제한 가설 1건" in section


def test_coverage_status_display(client, engine):
    from service.model.report import AreaCoverage, Coverage

    coverage = Coverage(
        run=AreaCoverage(status="checked", summary="본 것"),
        config=AreaCoverage(status="not_checked", summary="사유"),
        wiring=AreaCoverage(status="not_applicable", summary="사유2"),
    )
    text = client.get(f"/dev/issues/{_done_issue(engine, coverage=coverage)}").text
    assert 'aria-label="확인함"' in text
    assert ">확인함<" not in text
    assert "확인하지 못함" in text
    assert "해당 없음" in text


def test_single_investigation_has_no_previous_report_section(client, engine):
    text = client.get(f"/dev/issues/{_done_issue(engine)}").text
    assert "이전 보고서" not in text
    assert "#s-previous" not in text
    assert "이 보고서 보기" not in text


def test_old_report_lives_in_timeline_entry(client, engine):
    run = make_run(engine)
    inc = open_incident(engine, run)
    first = start_investigation(engine, run, inc, [])
    finish_investigation(engine, first, report(conclusion="옛 결론"))
    second = start_investigation(engine, run, inc, [])
    finish_investigation(engine, second, report(conclusion="새 결론"))
    text = client.get(f"/dev/issues/{inc}").text
    assert "이전 보고서" not in text
    assert f"<summary>R-{first:04d} 보고서</summary>" in text
    assert f"<summary>R-{second:04d} 보고서</summary>" not in text
    assert "옛 결론" in text
    assert f"report={first}" in text and "이 보고서 보기" in text
    old = client.get(f"/dev/issues/{inc}?report={first}").text
    assert f"<summary>R-{second:04d} 보고서</summary>" in old
    assert f"<summary>R-{first:04d} 보고서</summary>" not in old


def test_empty_hypotheses_and_issues_show_no_record(client, engine):
    inc = _done_issue(engine)
    text = client.get(f"/dev/issues/{inc}").text
    section = text.split("4. 가설 판정")[1].split("5. 확인한 범위")[0]
    assert "기록 없음" in section
    run = make_run(engine)
    inc2 = open_incident(engine, run)
    inv = start_investigation(engine, run, inc2, [])
    finish_investigation(engine, inv, report().model_copy(update={"issues": []}))
    text = client.get(f"/dev/issues/{inc2}").text
    section = text.split("2. 문제")[1].split("3. 사전 확인")[0]
    assert "기록 없음" in section
    assert "<table" not in section


def test_old_report_timeline_escapes_and_has_single_fold(client, engine):
    run = make_run(engine)
    inc = open_incident(engine, run)
    first = start_investigation(engine, run, inc, [])
    finish_investigation(engine, first, report(conclusion="<b>옛</b> 결론"))
    second = start_investigation(engine, run, inc, [])
    finish_investigation(engine, second, report())
    text = client.get(f"/dev/issues/{inc}").text
    assert "&lt;b&gt;옛&lt;/b&gt; 결론" in text
    assert "<b>옛</b>" not in text
    assert f"보고서 R-{first:04d}" not in text
    assert text.count(f"R-{first:04d} 보고서") == 1


def test_failed_old_investigation_has_no_fold(client, engine):
    run = make_run(engine)
    inc = open_incident(engine, run)
    first = start_investigation(engine, run, inc, [])
    finish_investigation(engine, first, None, status="failed")
    second = start_investigation(engine, run, inc, [])
    finish_investigation(engine, second, report())
    text = client.get(f"/dev/issues/{inc}").text
    assert "조사 실패" in text
    assert f"R-{first:04d} 보고서" not in text


def test_timeline_shows_message_name_not_number(client, engine, clock):
    from api.viewmodels.issues import build_issue_detail

    run = make_run(engine)
    inc = open_incident(engine, run)
    store = AlertStore(engine)
    store.add(kind="M1", channel="C", text="a", sent_at=at(10), incident_id=inc)
    store.add(kind="M1u", channel="C", text="b", sent_at=at(20), incident_id=inc)
    store.add(kind="M99", channel="C", text="c", sent_at=at(30), incident_id=inc)
    labels = [e.label for e in build_issue_detail(engine, inc, None, clock.now).timeline]
    assert "Slack · 이슈 알림" in labels
    assert "Slack · 원인 갱신" in labels
    assert "Slack · M99" in labels
    assert "Slack M1" not in labels


def _timeline_text(engine, clock, stored):
    from api.viewmodels.issues import build_issue_detail

    inc = open_incident(engine, make_run(engine))
    AlertStore(engine).add(kind="M1", channel="C", text=stored, sent_at=at(10), incident_id=inc)
    return [e.text for e in build_issue_detail(engine, inc, None, clock.now).timeline][-1]


def test_timeline_restores_slack_escapes_and_html_still_escapes(client, engine):
    inc = open_incident(engine, make_run(engine))
    stored = "a &amp;&amp; b &lt;c&gt; &lt;script&gt;alert(1)&lt;/script&gt; &quot;"
    AlertStore(engine).add(kind="M1", channel="C", text=stored, sent_at=at(10), incident_id=inc)
    html = client.get(f"/dev/issues/{inc}").text
    assert "a &amp;&amp; b &lt;c&gt;" in html  # 화면에는 a && b <c> 로 보인다
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)" in html
    assert "&amp;amp;" not in html
    assert "&amp;quot;" in html  # 다른 엔티티는 되돌리지 않는다


@pytest.mark.parametrize(
    "stored",
    ["password=ab&lt;cdSECRET", "api_key=abc&lt;SECRET", "Authorization: ab&amp;cdSECRET"],
)
def test_timeline_redacts_after_restoring_escapes(engine, clock, stored):
    assert "SECRET" not in _timeline_text(engine, clock, stored)


# ── 확인(ack) 표시 (#156) ──


def test_timeline_labels_ack_message(engine, clock):
    from api.viewmodels.issues import ALERT_LABEL, build_issue_detail

    inc = open_incident(engine, make_run(engine))
    AlertStore(engine).add(
        kind="ACK", channel="C", text="✅ <@U1> 님이 확인했습니다", sent_at=at(10), incident_id=inc
    )

    labels = [e.label for e in build_issue_detail(engine, inc, None, clock.now).timeline]

    assert ALERT_LABEL["ACK"] == "확인" and "Slack · 확인" in labels


def test_critical_open_issue_shows_unacked(client, engine):
    inc = open_incident(engine, make_run(engine))

    html = client.get(f"/dev/issues/{inc}").text

    assert "미확인" in html


def test_acked_issue_shows_time_and_masked_name(client, engine):
    from datetime import UTC, datetime

    from core.db import IncidentAckStore

    inc = open_incident(engine, make_run(engine))
    IncidentAckStore(engine).ack(
        inc,
        "U1",
        "홍길동 sk-abcdefghijklmnopqrstuvwxyz123456",
        datetime(2026, 11, 2, 0, 3, tzinfo=UTC),
    )

    html = client.get(f"/dev/issues/{inc}").text

    assert "확인 09:03 · 홍길동" in html and "미확인" not in html
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in html


def test_warning_and_resolved_issues_show_no_ack_state(client, engine):
    run = make_run(engine)
    warning = open_incident(engine, run, severity="warning")
    resolved = open_incident(engine, run)
    IncidentStore(engine).resolve(resolved, make_run(engine, start=at(60)), at(60))

    assert "미확인" not in client.get(f"/dev/issues/{warning}").text
    assert "미확인" not in client.get(f"/dev/issues/{resolved}").text


def test_cause_chain_shows_order_provenance_and_escaped_observation(client, engine):
    from service.model import Report

    run = make_run(engine)
    inc = open_incident(engine, run)
    ev = add_evidence(engine, run)
    inv = start_investigation(engine, run, inc, [])
    data = report(evidence_ids=[ev]).model_dump()
    data["cause_chain"] = [
        {
            "service": {"kind": "Service", "name": "agent-svc"},
            "observation": "<script>429 수신</script>",
            "evidence_ids": [ev],
            "error_from_downstream": True,
        }
    ]
    finish_investigation(engine, inv, Report.model_validate(data))
    text = client.get(f"/dev/issues/{inc}").text
    assert "호출 체인" in text and "Service/agent-svc" in text
    assert "하위 서비스 오류 전달" in text
    assert "&lt;script&gt;429 수신&lt;/script&gt;" in text
    assert f"/dev/checks/{run}?ev=1" in text


def test_log_fact_lines_render_in_terminal_box(client, engine):
    from service.model import Fact

    run = make_run(engine)
    inc = open_incident(engine, run)
    ev = add_evidence(engine, run, data={"lines": ["ERROR 429"], "match_count": 1})
    other = add_evidence(engine, run, kind="resource", data={"phase": "Running"})
    facts = [
        Fact(
            statement="get_pod_logs: 오류 2건\n  ERROR 429 Too Many\n  Bearer abcdefgh12345678",
            evidence_ids=[ev],
        ),
        Fact(statement="상태 확인\n  둘째 줄", evidence_ids=[other]),
    ]
    inv = start_investigation(engine, run, inc, [])
    finish_investigation(engine, inv, report(evidence_ids=(ev,), preliminary_facts=facts))
    text = client.get(f"/dev/issues/{inc}").text
    box = text[text.index('class="term"') : text.index("</pre>", text.index('class="term"'))]
    assert "ERROR 429 Too Many" in box
    assert "abcdefgh12345678" not in text
    assert "get_pod_logs: 오류 2건" in text and "get_pod_logs: 오류 2건" not in box
    assert text.count('class="term"') == 1  # 로그가 아닌 사실은 그대로 글로 보인다


def test_log_fact_keeps_inner_double_space_and_strips_only_leading_indent(client, engine):
    from service.model import Fact

    run = make_run(engine)
    inc = open_incident(engine, run)
    ev = add_evidence(engine, run, data={"lines": ["x"], "match_count": 1})
    facts = [Fact(statement="get_pod_logs: 오류 1건\n  ERROR  x  y\nWARN  z", evidence_ids=[ev])]
    inv = start_investigation(engine, run, inc, [])
    finish_investigation(engine, inv, report(evidence_ids=(ev,), preliminary_facts=facts))
    text = client.get(f"/dev/issues/{inc}").text
    box = text[text.index('class="term"') : text.index("</pre>", text.index('class="term"'))]
    assert ">ERROR  x  y\nWARN  z<" in box + "<"


def test_issue_list_date_range_kst(client, engine):
    run = make_run(engine)
    inc = open_incident(engine, run)  # 11/02 14:05 KST
    assert f"#{inc}" in client.get("/dev/issues?from=2026-11-02&to=2026-11-02").text
    assert 'name="from" value="2026-11-02"' in client.get("/dev/issues?from=2026-11-02").text
    assert client.get("/dev/issues?from=2026-11-03").text.count('class="irow') == 0
    assert client.get("/dev/issues?to=2026-11-01").text.count('class="irow') == 0
    assert client.get("/dev/issues?to=2026-11-02").text.count('class="irow') == 1
    # 잘못된 값·거꾸로 된 기간은 필터 없음과 같다
    for query in ("from=2026-13-40", "from=2026-11-05&to=2026-11-01", "to=abc"):
        response = client.get(f"/dev/issues?{query}")
        assert response.status_code == 200
        assert response.text.count('class="irow') == 1, query
    # 날짜가 빠른 선택보다 우선한다
    assert (
        client.get("/dev/issues?days=1&from=2026-11-02&to=2026-11-02").text.count('class="irow')
        == 1
    )
    # 상세로 가는 링크에도 기간이 남는다
    assert "from=2026-11-02" in client.get("/dev/issues?from=2026-11-02&to=2026-11-02").text
