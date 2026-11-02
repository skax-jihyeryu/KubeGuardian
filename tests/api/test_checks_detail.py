"""점검 상세 (UI 정의서 §6.4): 네 상황, 근거 서랍, 이스케이프·마스킹, 404, 폴링."""

from datetime import timedelta

import pytest

from core.db import CheckResultStore, InvestigationQueue
from service.model import Signal
from tests.api.seed import (
    add_evidence,
    at,
    fail_chat_symptom,
    finish_check,
    finish_investigation,
    link_incident,
    make_run,
    open_incident,
    report,
    start_investigation,
    start_running_check,
    step,
)

POLL = 'hx-trigger="every 3s"'


def detail(client, run, query=""):
    return client.get(f"/p/dev/checks/{run}{query}")


def _chat_failed(engine, trigger="schedule"):
    """기능 점검 실패로 이슈 #1이 열린 점검."""
    run = make_run(engine, trigger)
    ev = add_evidence(engine, run, kind="app_api", subject="scenario/chat")
    finish_check(
        engine,
        run,
        path="B",
        severity="critical",
        symptoms=[fail_chat_symptom(ev)],
        reason="기능 점검 chat 실패",
    )
    return run, ev


# ── 상황 1: 조사 중 ──────────────────────────────────────────────


@pytest.fixture
def investigating(engine):
    run, ev = _chat_failed(engine)
    inc = open_incident(engine, run)
    ev2 = add_evidence(engine, run, subject="dev/Pod/agent-x")
    iid = start_investigation(
        engine,
        run,
        inc,
        [
            step("preliminary", 10, evidence_ids=[ev]),
            step("plan", 12),
            step(
                "tools",
                14,
                tool="get_pod_logs",
                args={"name": "agent-x", "grep": "429"},
                evidence_ids=[ev2],
            ),
            step("tools", 16, tool="probe", args={"service": "agent"}),
        ],
    )
    return run, inc, iid, ev, ev2


def test_investigating_shows_progress_and_issue_link_but_no_conclusion(client, investigating):
    run, inc, *_ = investigating
    res = detail(client, run)
    assert res.status_code == 200
    text = res.text
    assert "조사 중" in text
    assert "도구 호출" in text and "2/8" in text  # 분모는 문제당 예산
    assert f"이슈 #{inc}" in text
    assert f'href="/p/dev/issues/{inc}"' in text
    assert "1차 판정" in text
    assert "조사 시작" in text  # 조사 판단


def test_investigating_polls_only_the_steps_fragment(client, investigating):
    run, *_ = investigating
    text = detail(client, run).text
    assert text.count(POLL) == 1
    assert f'hx-get="/p/dev/checks/{run}/steps' in text


def test_steps_fragment_lists_tool_calls_with_evidence_links(client, investigating):
    run, _, _, ev, ev2 = investigating
    res = client.get(f"/p/dev/checks/{run}/steps")
    assert res.status_code == 200
    assert "<html" not in res.text  # 조각
    assert "get_pod_logs" in res.text and "probe" in res.text
    assert f"?ev={int(ev2[3:])}" in res.text
    assert POLL in res.text


def test_polling_stops_when_investigation_finishes(client, engine, investigating):
    run, _, iid, ev, _ = investigating
    finish_investigation(engine, iid, report(evidence_ids=(ev,)))
    assert POLL not in detail(client, run).text
    res = client.get(f"/p/dev/checks/{run}/steps?live=1")
    assert POLL not in res.text
    assert res.headers.get("HX-Refresh") == "true"  # 머리·판정도 새 상태로


def test_polling_stops_when_running_check_becomes_halted(client, engine, clock):
    run = make_run(engine)
    start_running_check(engine, run)
    assert POLL in detail(client, run).text
    live = client.get(f"/p/dev/checks/{run}/steps?live=1")
    assert POLL in live.text and "HX-Refresh" not in live.headers

    clock.now = at(1) + timedelta(seconds=271)  # 마지막 진행에서 잠금 만료(270초)가 지남
    stopped = client.get(f"/p/dev/checks/{run}/steps?live=1")
    assert POLL not in stopped.text
    assert stopped.headers.get("HX-Refresh") == "true"
    page = detail(client, run).text
    assert POLL not in page
    assert "중단" in page


def test_investigation_stuck_running_is_called_stalled_and_stops_polling(
    client, clock, investigating
):
    run, *_ = investigating
    clock.now = at(10) + timedelta(minutes=11)  # 작업자 정리(10분)를 넘김
    text = detail(client, run).text
    assert POLL not in text
    assert "조사가 중단됐습니다" in text


def test_running_check_without_investigation_shows_check_progress(client, engine):
    run = make_run(engine)
    start_running_check(engine, run, with_phases=1)
    text = detail(client, run).text
    assert "점검 규칙 점검" in text
    assert "점검 중" in text
    assert POLL in text


def test_failed_check_has_no_polling_and_says_failed(client, engine):
    run = make_run(engine)
    store = CheckResultStore(engine)
    store.start(run, at(0))
    store.finish(run, "failed", at(3), None, [], [], error="ServiceException")
    text = detail(client, run).text
    assert POLL not in text
    assert "점검 실패" in text and "ServiceException" in text


# ── 상황 2: 조사 끝남 ────────────────────────────────────────────


@pytest.fixture
def investigated(engine, investigating):
    run, inc, iid, ev, ev2 = investigating
    finish_investigation(engine, iid, report(judgement="confirmed", evidence_ids=(ev, ev2)))
    return run, inc


def test_investigated_links_to_issue_and_has_no_report_body(client, investigated):
    run, inc = investigated
    text = detail(client, run).text
    assert f"이슈 #{inc}에서 결론 보기" in text
    assert "llm-gateway 할당량 소진이 원인" not in text  # 결론·조치 본문은 이슈 상세에
    assert "권장 조치" not in text
    assert POLL not in text


def test_judgement_level_not_confidence(client, investigated):
    run, _ = investigated
    text = detail(client, run).text
    assert "판단: 확정" in text
    assert "확신도" not in text


@pytest.mark.parametrize(
    ("judgement", "label", "dots"),
    [("confirmed", "확정", 3), ("candidate", "후보", 2), ("guess", "추정", 1)],
)
def test_judgement_dots_are_3_2_1(client, engine, investigating, judgement, label, dots):
    run, _, iid, ev, _ = investigating
    finish_investigation(engine, iid, report(judgement=judgement, evidence_ids=(ev,)))
    text = detail(client, run).text
    assert f"판단: {label}" in text
    assert text.count("<i></i>") >= dots and text.count('<i class="e"></i>') == 3 - dots


def test_incomplete_or_failed_investigation_is_said_plainly(client, engine, investigating):
    run, _, iid, *_ = investigating
    finish_investigation(engine, iid, None, status="failed", error="LLM_CALL_FAILED")
    text = detail(client, run).text
    assert "조사 실패" in text
    assert POLL not in text


# ── 상황 3: 이상 없음 ────────────────────────────────────────────


def test_calm_check(client, engine):
    run = make_run(engine)
    finish_check(
        engine,
        run,
        path="C",
        signals=[
            Signal(rule_id="S01", verdict="passed", subject="dev/Service/a"),
            Signal(rule_id="S01", verdict="passed", subject="dev/Service/b"),
            Signal(rule_id="H01", verdict="detected", subject="dev/Deployment/a", severity="info"),
        ],
    )
    text = detail(client, run).text
    assert "이상 없음" in text
    assert "조사 안 함 — 장애 신호 없음" in text
    assert "통과 2건" in text
    assert "확인하지 못함" not in text
    assert "조사 시작" not in text
    assert POLL not in text
    assert "해당 없음" in text  # 조사 단계는 붙지 않는다


# ── 상황 4: 일부 확인하지 못함 ───────────────────────────────────


def test_unknown_items_show_reason_and_gate_stays_not_investigating(client, engine):
    run = make_run(engine)
    finish_check(
        engine,
        run,
        path="C",
        signals=[
            Signal(
                rule_id="W02",
                verdict="unknown",
                subject="dev/Deployment/agent-v2",
                message="대상 Deployment가 클러스터에 없어 롤아웃 상태를 읽지 못함",
            ),
            Signal(rule_id="S01", verdict="passed", subject="dev/Service/a"),
        ],
    )
    CheckResultStore(engine)  # 결과 행은 이미 있다
    text = detail(client, run).text
    assert "확인하지 못함" in text
    assert "W02" in text
    assert "대상 Deployment가 클러스터에 없어" in text
    assert "조사 안 함" in text
    band = client.get("/p/dev/checks").text
    assert f'href="/p/dev/checks/{run}#s-judge"' in band  # 상단 띠 칩의 도착점
    assert "확인하지 못함 1" in band


# ── 조사 시작 표기는 gate_path만으로 정하지 않는다 ───────────────


def test_direct_check_does_not_start_investigation_even_with_gate_path_b(client, engine):
    run, _ = _chat_failed(engine, trigger="ui")
    text = detail(client, run).text
    assert "조사 안 함" in text and "직접 점검" in text
    assert "조사 시작" not in text
    assert "조사 중" not in text
    assert POLL not in text


def test_continued_check_does_not_start_investigation(client, engine):
    first, _ = _chat_failed(engine)
    inc = open_incident(engine, first)
    run, _ = _chat_failed(engine)
    link_incident(engine, inc, run, "continued")
    text = detail(client, run).text
    assert "조사 안 함" in text and "이어짐" in text
    assert f"이슈 #{inc}" in text
    assert "조사 시작" not in text
    assert POLL not in text


def test_opened_issue_waiting_for_worker_polls_but_is_not_called_running(client, engine):
    run, _ = _chat_failed(engine)
    inc = open_incident(engine, run)
    InvestigationQueue(engine).put(inc, run, {}, at(5))  # 이슈의 조사 상태가 pending이 된다
    text = detail(client, run).text
    assert "조사 대기" in text
    assert "조사 중" not in text
    assert POLL in text  # 곧 작업자가 시작한다


def test_old_check_without_result_says_no_record(client, engine):
    run = make_run(engine)
    res = detail(client, run)
    assert res.status_code == 200
    assert "기록 없음" in res.text
    assert POLL not in res.text


# ── 404 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path", ["/p/dev/checks/999", "/p/dev/checks/999/steps", "/p/dev/checks/abc"]
)
def test_missing_check_is_404_not_500(client, path):
    assert client.get(path).status_code == 404


# ── 근거 서랍 ───────────────────────────────────────────────────


def test_evidence_drawer_opens_with_masked_source(client, engine):
    run = make_run(engine)
    finish_check(engine, run, path="C")
    ev = add_evidence(
        engine,
        run,
        subject="dev/Pod/agent-x",
        source="GET /api/v1/namespaces/dev/pods/agent-x/log",
        data={"log": "WARN Authorization: Bearer abcdefgh12345678", "db_password": "hunter2"},
    )
    n = int(ev[3:])
    res = detail(client, run, f"?ev={n}")
    assert res.status_code == 200
    assert 'class="drawer"' in res.text
    assert f"근거 {n}" in res.text
    assert "log_excerpt" in res.text
    assert "dev/Pod/agent-x" in res.text
    assert "hunter2" not in res.text and "abcdefgh12345678" not in res.text
    assert "***" in res.text  # 원문은 마스킹되어 보인다
    assert f"/p/dev/checks/{run}?ev={n}" in res.text  # 단독 페이지 링크


def test_page_without_ev_has_no_open_drawer(client, engine):
    run = make_run(engine)
    finish_check(engine, run, path="C")
    assert 'class="drawer"' not in detail(client, run).text


@pytest.mark.parametrize("ev", ["999", "0", "abc", "-1", "1.5", "ev-0001", "99999999999999999999"])
def test_bad_or_unknown_ev_is_404(client, engine, ev):
    run = make_run(engine)
    finish_check(engine, run, path="C")
    assert detail(client, run, f"?ev={ev}").status_code == 404


def test_other_checks_evidence_is_404(client, engine):
    a = make_run(engine)
    finish_check(engine, a, path="C")
    b = make_run(engine)
    finish_check(engine, b, path="C")
    other = int(add_evidence(engine, b)[3:])
    assert detail(client, b, f"?ev={other}").status_code == 200
    assert detail(client, a, f"?ev={other}").status_code == 404


def test_evidence_without_a_check_is_404(client, engine):
    run = make_run(engine)
    finish_check(engine, run, path="C")
    orphan = int(add_evidence(engine, None)[3:])
    assert detail(client, run, f"?ev={orphan}").status_code == 404


# ── 이스케이프·마스킹 ────────────────────────────────────────────

XSS = "<script>alert(1)</script>"


def test_untrusted_strings_are_escaped_everywhere(client, engine):
    run = make_run(engine)
    ev = add_evidence(
        engine, run, subject=f"dev/Pod/{XSS}", source=XSS, data={"log": XSS, XSS: "x"}
    )
    finish_check(
        engine,
        run,
        path="A",
        severity="critical",
        signals=[
            Signal(
                rule_id="W02",
                verdict="unknown",
                subject=f"dev/Deployment/{XSS}",
                message=XSS,
            ),
            Signal(
                rule_id="P03",
                verdict="detected",
                subject=f"dev/Pod/{XSS}",
                severity="critical",
                message=XSS,
                values={"reason": XSS},
                evidence_id=ev,
            ),
        ],
        reason=XSS,
    )
    inc = open_incident(engine, run)
    start_investigation(
        engine,
        run,
        inc,
        [
            step(
                "tools",
                14,
                tool="probe",
                args={"service": XSS, "token": "supersecretvalue1"},
                evidence_ids=[ev],
            )
        ],
        gate_reason=XSS,
    )
    n = int(ev[3:])
    for page in (
        detail(client, run),
        detail(client, run, f"?ev={n}"),
        client.get(f"/p/dev/checks/{run}/steps"),
    ):
        assert page.status_code == 200
        assert "<script>alert" not in page.text
        assert "&lt;script&gt;" in page.text
        assert "supersecretvalue1" not in page.text


def test_stored_secret_values_in_step_args_show_masked(client, engine):
    run, ev = _chat_failed(engine)
    inc = open_incident(engine, run)
    start_investigation(
        engine,
        run,
        inc,
        [
            step(
                "tools",
                14,
                tool="probe",
                args={"password": "hunter2", "url": "http://u:pw12345@h/"},
            )
        ],
    )
    text = client.get(f"/p/dev/checks/{run}/steps").text
    assert "hunter2" not in text and "pw12345" not in text
    assert "***" in text


# ── 근거 목록·마스킹된 판정 값 ───────────────────────────────────


def test_evidence_table_lists_this_checks_evidence_with_links(client, engine):
    run, ev = _chat_failed(engine)
    other_run = make_run(engine)
    other = add_evidence(engine, other_run)
    text = detail(client, run).text
    assert f"?ev={int(ev[3:])}" in text
    assert f"?ev={int(other[3:])}" not in text
    assert "근거" in text


def test_unknown_advice_is_shown_with_reason(client, engine):
    from service.model import GateResult

    run = make_run(engine)
    store = CheckResultStore(engine)
    store.start(run, at(0))
    store.finish(
        run,
        "done",
        at(5),
        GateResult(path="C", reason="수집 일부 실패"),
        [
            Signal(
                rule_id="H01",
                verdict="unknown",
                subject="dev/Deployment/agent",
                message="Pod 수집 권한이 없어 readinessProbe를 확인하지 못했습니다",
            )
        ],
        [],
        rules=["H01"],
    )
    page = detail(client, run).text
    assert "점검 규칙 H01" in page
    assert "Pod 수집 권한이 없어 readinessProbe를 확인하지 못했습니다" in page
    assert "확인하지 못함 1" in page
    assert "개선 권고 1건" not in page
