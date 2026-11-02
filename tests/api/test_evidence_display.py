import pytest

from api.viewmodels.checks import build_check_detail
from tests.api.seed import add_evidence, at, finish_check, make_run


@pytest.mark.parametrize(
    ("kind", "data", "summary", "label"),
    [
        (
            "detector",
            {"verdict": "detected", "message": "Endpoint 없음"},
            "Endpoint 없음",
            "규칙 점검 결과",
        ),
        ("app_api", {"status": 502, "latency_ms": 20}, "HTTP 502", "기능 점검·앱 API 호출"),
        (
            "app_api",
            {"scenario": "chat", "failed_step": 2, "steps": [{}, {}]},
            "실패 단계 2",
            "기능 점검·앱 API 호출",
        ),
        ("resource", {"kind": "Pod", "count": 0, "items": []}, "Pod 0개", "리소스 설정·상태 조회"),
        ("event", {"count": 3, "warnings": 2, "events": []}, "이벤트 3건", "이벤트"),
        ("log_excerpt", {"line_count": 0, "lines": []}, "로그 0줄", "Pod 로그"),
        ("probe_result", {"ok": 1, "fail": 2}, "성공 1·실패 2", "연결 확인"),
        ("resource", {}, "요약할 결과 필드 없음", "리소스 설정·상태 조회"),
        ("resource", {"error": "password=hunter2"}, "조회 실패", "리소스 설정·상태 조회"),
    ],
)
def test_evidence_result_target_and_description(client, engine, kind, data, summary, label):
    run = make_run(engine)
    finish_check(engine, run, path="C")
    ev = add_evidence(engine, run, kind=kind, data=data, subject="dev/Pod/target")
    row = build_check_detail(engine, "dev", run, at(60)).evidence[0]
    assert summary in row.content
    assert row.subject == "dev/Pod/target"
    assert row.kind == label
    html = client.get(f"/dev/checks/{run}?ev={int(ev[3:])}").text
    assert "<th>결과 요약</th><th>대상</th>" in html
    assert "전체 점검에 걸친 수집 순서" in html
    assert label in html
    assert "hunter2" not in html


@pytest.mark.parametrize(
    "data",
    [
        {"scenario": "chat"},
        {"scenario": "chat", "steps": [{}]},
        {"scenario": "chat", "failed_step": None},
    ],
)
def test_incomplete_scenario_evidence_does_not_imply_success(engine, data):
    run = make_run(engine)
    finish_check(engine, run, path="C")
    add_evidence(engine, run, kind="app_api", data=data)
    row = build_check_detail(engine, "dev", run, at(60)).evidence[0]
    assert row.content == "요약할 결과 필드 없음"
