"""이슈 화면은 저장된 보고서만 읽고 다른 이슈의 보고서를 섞지 않는다."""

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
    assert "할당량 소진" in client.get("/dev/issues?q=할당량").text
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
        ("Slack M2", 1),
        ("이어짐 점검", 2),
    ]
