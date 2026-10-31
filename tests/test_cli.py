import json

import pytest
from langchain_core.messages import AIMessage

import cli
from core.config import KubeGuardianConfig, get_setting
from core.error.service_exception import ErrorCode, ServiceException
from tests.service.agent.history_qa.fakes import ScriptedLLM as HistoryLLM
from tests.service.agent.investigator.test_v1_graph import ScriptedLLM
from tests.service.checks.detectors.cases import FixtureReader, raw_cluster
from tests.service.tools.fakes import FakeHttp

CONFIG_YAML = """
target: {namespace: dev}
core_paths: {chat: [gateway, agent]}
"""


PLAN = '{"hypotheses": [], "order": ["run", "config", "wiring", "logs"]}'


def _config(tmp_path):
    path = tmp_path / "kubeguardian.yaml"
    path.write_text(CONFIG_YAML, encoding="utf-8")
    return str(path)


SCENARIO_YAML = (
    CONFIG_YAML
    + """
api_scenarios:
  - name: chat
    feature: chat
    steps:
      - call: {via: gateway, path: /chat, method: GET}
"""
)


def _scenario_config(tmp_path):
    path = tmp_path / "kubeguardian.yaml"
    path.write_text(SCENARIO_YAML, encoding="utf-8")
    return str(path)


def _args(tmp_path):
    return ["check", "--config", _config(tmp_path), "--db", str(tmp_path / "kg.db")]


def test_check_prints_rule_results_and_gate(tmp_path, capsys):
    code = cli.main(
        _args(tmp_path), make_reader=lambda cfg: FixtureReader(*raw_cluster("s01/detected"))
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "S01 selector에 맞는 Pod 없음" in out
    assert "적발(critical)  dev/Service/agent" in out
    assert '"selector": {"app": "agnet"}' in out
    assert "게이트: 경로 A (critical)" in out


def test_check_shows_unknown_for_collection_failure(tmp_path, capsys):
    objects, _ = raw_cluster()
    code = cli.main(
        _args(tmp_path), make_reader=lambda cfg: FixtureReader(objects, {"Secret": "403 Forbidden"})
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "수집 실패: Secret (403 Forbidden)" in out
    assert "확인하지 못함  dev" in out
    assert "게이트: 경로 C" in out


def test_missing_config_exits_with_client_error(tmp_path, capsys):
    code = cli.main(
        ["check", "--config", str(tmp_path / "none.yaml"), "--db", str(tmp_path / "x.db")]
    )
    assert code == 2
    assert str(ErrorCode.BAD_REQUEST.code) in capsys.readouterr().err


def test_service_exception_sets_exit_code(tmp_path, capsys):
    def fail(cfg):
        raise ServiceException(ErrorCode.COLLECT_FAILED, "kubeconfig 없음")

    assert cli.main(_args(tmp_path), make_reader=fail) == 1
    assert "91001" in capsys.readouterr().err


def test_check_shows_symptom_results_and_path_b(tmp_path, capsys):
    argv = ["check", "--config", _scenario_config(tmp_path), "--db", str(tmp_path / "kg.db")]
    code = cli.main(
        argv,
        make_reader=lambda cfg: FixtureReader(*raw_cluster()),
        make_http=lambda cfg: FakeHttp({("gateway", "/chat"): 500}),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "증상 감지" in out
    assert "실패(critical)  chat  시나리오 chat: 1단계 중 1단계 실패" in out
    assert out.index("증상 감지") < out.index("게이트:")
    assert "게이트: 경로 B (critical)" in out


def test_check_shows_passing_scenario(tmp_path, capsys):
    argv = ["check", "--config", _scenario_config(tmp_path), "--db", str(tmp_path / "kg.db")]
    cli.main(
        argv,
        make_reader=lambda cfg: FixtureReader(*raw_cluster()),
        make_http=lambda cfg: FakeHttp({("gateway", "/chat"): 200}),
    )
    out = capsys.readouterr().out
    assert "성공  chat  시나리오 chat: 1단계 모두 성공" in out
    assert "게이트: 경로 C" in out


def test_check_shows_scenario_that_could_not_run_with_reason(tmp_path, capsys):
    # Service 목록을 읽지 못하면 대상 Service가 있는지 알 수 없다
    argv = ["check", "--config", _scenario_config(tmp_path), "--db", str(tmp_path / "kg.db")]
    objects, _ = raw_cluster()
    code = cli.main(
        argv,
        make_reader=lambda cfg: FixtureReader(objects, {"Service": "api down"}),
        make_http=lambda cfg: FakeHttp(),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "확인하지 못함  chat  조회 실패:" in out
    assert "증상 감지 확인하지 못함 1건" in out
    assert "게이트: 경로 C" in out


def test_check_shows_missing_target_service_as_failed_symptom(tmp_path, capsys):
    path = tmp_path / "kubeguardian.yaml"
    path.write_text(SCENARIO_YAML.replace("via: gateway", "via: nowhere"), encoding="utf-8")
    argv = ["check", "--config", str(path), "--db", str(tmp_path / "kg.db")]
    cli.main(
        argv,
        make_reader=lambda cfg: FixtureReader(*raw_cluster()),
        make_http=lambda cfg: FakeHttp(),
    )
    out = capsys.readouterr().out
    assert "실패(critical)  chat" in out
    assert "게이트: 경로 B (critical)" in out


def test_default_http_gets_accounts_from_accounts_dir(tmp_path, monkeypatch):
    secret = tmp_path / "accounts" / "kg-test-user"
    secret.mkdir(parents=True)
    (secret / "id").write_text("tester\n")
    monkeypatch.setenv("KG_ACCOUNTS_DIR", str(tmp_path / "accounts"))
    get_setting.cache_clear()
    try:
        http = cli._http(KubeGuardianConfig.model_validate({"target": {"namespace": "dev"}}))
        assert http.account("kg-test-user") == {"id": "tester"}
    finally:
        get_setting.cache_clear()


# ── kg investigate ──


def _investigate_args(tmp_path, *extra):
    return ["investigate", *extra, "--config", _config(tmp_path), "--db", str(tmp_path / "kg.db")]


def _llm_factory(llm):
    return lambda model: llm


def _scripted():
    triage = json.dumps(
        {
            "issues": [
                {
                    "rank": 1,
                    "title": "gateway 주소 설정 오류",
                    "impact": "core_path_broken",
                    "confidence": "high",
                }
            ],
            "runbook": "service-unreachable",
        }
    )
    report = json.dumps(
        {
            "conclusion": "gateway 주소 env가 localhost",
            "confidence": "high",
            "impact_scope": "전체",
            "coverage": {
                a: {"status": "not_applicable", "summary": "테스트"}
                for a in ("run", "config", "wiring", "logs")
            },
            "issues": [
                {
                    "rank": 1,
                    "title": "gateway 주소 설정 오류",
                    "impact": "core_path_broken",
                    "confidence": "high",
                    "cause_resource": {"kind": "Deployment", "name": "gateway"},
                }
            ],
            "evidence_ids": ["ev-0001"],  # 새 DB의 첫 근거 (규칙 점검이 저장한 S04 적발)
        }
    )
    look = AIMessage(
        content="",
        tool_calls=[
            {"name": "get_resource", "args": {"kind": "Deployment", "name": "gateway"}, "id": "c1"}
        ],
    )  # 원인 리소스의 설정을 직접 본다 (멈춤 검사)
    return ScriptedLLM(
        script=[triage, PLAN, look, AIMessage(content="조사 완료"), report], calls=[]
    )


def test_investigate_runs_agent_when_gate_opens_and_prints_report(tmp_path, capsys):
    llm = _scripted()
    code = cli.main(
        _investigate_args(tmp_path, "채팅이 안 돼요"),
        make_reader=lambda cfg: FixtureReader(*raw_cluster("s04/localhost")),
        make_http=lambda cfg: FakeHttp(),
        make_llm=_llm_factory(llm),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "게이트: 경로 A" in out
    assert "조사 상태: done" in out
    assert "결론: gateway 주소 env가 localhost" in out
    assert "1순위: (추정) gateway 주소 설정 오류 (critical)" in out
    assert "근거 1건" in out
    assert any("채팅이 안 돼요" in str(m.content) for m in llm.calls[0])


def test_investigate_symptom_only_opens_path_b(tmp_path, capsys):
    code = cli.main(
        _investigate_args(tmp_path, "채팅이 안 돼요"),
        make_reader=lambda cfg: FixtureReader(*raw_cluster()),
        make_http=lambda cfg: FakeHttp(),
        make_llm=_llm_factory(_scripted()),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "게이트: 경로 B (warning) — 증상: 사용자 증상" in out
    assert "조사 상태:" in out


def test_investigate_skips_agent_on_path_c(tmp_path, capsys):
    def no_llm(model):
        raise AssertionError("경로 C는 조사하지 않는다")

    code = cli.main(
        _investigate_args(tmp_path),
        make_reader=lambda cfg: FixtureReader(*raw_cluster()),
        make_http=lambda cfg: FakeHttp(),
        make_llm=no_llm,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "게이트: 경로 C" in out
    assert "조사하지 않음" in out


def test_investigate_exits_1_when_the_agent_fails(tmp_path, capsys):
    llm = ScriptedLLM(script=[RuntimeError("boom")], calls=[])
    code = cli.main(
        _investigate_args(tmp_path),
        make_reader=lambda cfg: FixtureReader(*raw_cluster("s04/localhost")),
        make_http=lambda cfg: FakeHttp(),
        make_llm=_llm_factory(llm),
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "조사 상태: failed" in out


def test_blank_symptom_is_treated_as_no_symptom(tmp_path, capsys):
    def no_llm(model):
        raise AssertionError("공백 증상은 증상이 아니다")

    code = cli.main(
        _investigate_args(tmp_path, "   "),
        make_reader=lambda cfg: FixtureReader(*raw_cluster()),
        make_http=lambda cfg: FakeHttp(),
        make_llm=no_llm,
    )
    assert code == 0
    assert "게이트: 경로 C" in capsys.readouterr().out


def test_investigate_rejects_too_long_symptom(tmp_path, capsys):
    code = cli.main(
        _investigate_args(tmp_path, "가" * 501),
        make_reader=lambda cfg: FixtureReader(*raw_cluster()),
        make_http=lambda cfg: FakeHttp(),
    )
    assert code == 2
    assert str(ErrorCode.VALIDATION_ERROR.code) in capsys.readouterr().err


def _stored_triggers(tmp_path):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from core.db import create_db_engine
    from core.db.tables import CheckRunRow

    with Session(create_db_engine(str(tmp_path / "kg.db"))) as s:
        return list(s.scalars(select(CheckRunRow.trigger)))


def test_check_records_schedule_trigger(tmp_path):
    code = cli.main(
        [*_args(tmp_path), "--trigger", "schedule"],
        make_reader=lambda cfg: FixtureReader(*raw_cluster("s01/detected")),
    )
    assert code == 0
    assert _stored_triggers(tmp_path) == ["schedule"]


def test_check_trigger_defaults_to_check(tmp_path):
    cli.main(_args(tmp_path), make_reader=lambda cfg: FixtureReader(*raw_cluster("s01/detected")))
    assert _stored_triggers(tmp_path) == ["check"]


def test_check_rejects_unknown_trigger(tmp_path):
    with pytest.raises(SystemExit) as e:
        cli.main([*_args(tmp_path), "--trigger", "bogus"])
    assert e.value.code == 2


def test_investigate_records_schedule_trigger(tmp_path):
    cli.main(
        [*_investigate_args(tmp_path, "채팅이 안 돼요"), "--trigger", "schedule"],
        make_reader=lambda cfg: FixtureReader(*raw_cluster("s04/localhost")),
        make_http=lambda cfg: FakeHttp(),
        make_llm=_llm_factory(_scripted()),
    )
    assert _stored_triggers(tmp_path) == ["schedule"]


def _hold_lock(tmp_path, trigger="ui"):
    from datetime import UTC, datetime

    from core.db import CheckLockStore, create_db_engine
    from service.pipeline.check import CHECK_LOCK_TTL

    engine = create_db_engine(str(tmp_path / "kg.db"))
    CheckLockStore(engine).acquire(trigger, datetime.now(UTC), CHECK_LOCK_TTL)


def test_schedule_check_skips_quietly_when_another_check_runs(tmp_path, capsys):
    _hold_lock(tmp_path)

    code = cli.main(
        [*_args(tmp_path), "--trigger", "schedule"],
        make_reader=lambda cfg: FixtureReader(*raw_cluster(None)),
    )

    assert code == 0  # CronJob 실패로 보이지 않게
    assert "건너뜀" in capsys.readouterr().out
    assert _stored_triggers(tmp_path) == []


def test_direct_check_reports_conflict_when_another_check_runs(tmp_path, capsys):
    _hold_lock(tmp_path, trigger="schedule")

    code = cli.main(_args(tmp_path), make_reader=lambda cfg: FixtureReader(*raw_cluster(None)))

    assert code == 2
    assert "12001" in capsys.readouterr().err


def test_sigterm_runs_finally_so_the_lock_is_released():
    # activeDeadlineSeconds는 SIGTERM으로 끝낸다. 기본 처리로는 finally가 돌지 않아 잠금이 남는다
    import signal

    cli._exit_on_sigterm()
    handler = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(SystemExit) as e:
            handler(signal.SIGTERM, None)
        assert e.value.code == 128 + signal.SIGTERM
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


# ── 이슈 (#26) ──


def _schedule_investigate(tmp_path, llm_factory):
    return cli.main(
        [*_investigate_args(tmp_path), "--trigger", "schedule"],
        make_reader=lambda cfg: FixtureReader(*raw_cluster("s04/localhost")),
        make_http=lambda cfg: FakeHttp(),
        make_llm=llm_factory,
    )


def test_schedule_investigate_runs_once_then_skips_while_incident_continues(tmp_path, capsys):
    from core.db import IncidentStore, create_db_engine

    assert _schedule_investigate(tmp_path, _llm_factory(_scripted())) == 0
    first = capsys.readouterr().out
    assert "이슈 #1 새로 열림" in first and "조사 상태: done" in first

    def no_llm(model):
        raise AssertionError("같은 지문이 이어지면 조사하지 않는다")

    assert _schedule_investigate(tmp_path, no_llm) == 0
    second = capsys.readouterr().out
    assert "이슈 #1 진행 중" in second and "조사 생략:" in second

    incident = IncidentStore(create_db_engine(str(tmp_path / "kg.db"))).get(1)
    assert (incident.investigation, incident.final_severity) == ("done", "core_path_broken")


def test_direct_investigate_always_runs_even_with_open_incident(tmp_path, capsys):
    _schedule_investigate(tmp_path, _llm_factory(_scripted()))
    capsys.readouterr()

    llm = _scripted()  # 보고서가 인용한 ev-0001은 첫 DB 기준이라 결과 상태는 보지 않는다
    cli.main(
        _investigate_args(tmp_path),
        make_reader=lambda cfg: FixtureReader(*raw_cluster("s04/localhost")),
        make_http=lambda cfg: FakeHttp(),
        make_llm=_llm_factory(llm),
    )

    out = capsys.readouterr().out
    assert llm.calls and "조사 상태:" in out and "조사 생략" not in out


def test_check_prints_incident_resolution(tmp_path, capsys):
    cli.main(
        [*_args(tmp_path), "--trigger", "schedule"],
        make_reader=lambda cfg: FixtureReader(*raw_cluster("s01/detected")),
    )
    cli.main(
        [*_args(tmp_path), "--trigger", "schedule"],
        make_reader=lambda cfg: FixtureReader(*raw_cluster(None)),
    )

    assert "이슈 #1 해결" in capsys.readouterr().out


def test_worker_retries_incident_left_running_by_interrupted_cli(tmp_path, capsys, monkeypatch):
    # 조사 중 Pod가 끝나(SIGTERM) 기록하지 못한 이슈는 집힘이 만료된 뒤 작업자가 다시 조사한다
    from datetime import timedelta

    from core.db import IncidentStore, create_db_engine
    from service.pipeline import worker as worker_module

    def interrupted(model):
        raise SystemExit(143)

    with pytest.raises(SystemExit):
        _schedule_investigate(tmp_path, interrupted)
    store = IncidentStore(create_db_engine(str(tmp_path / "kg.db")))
    assert store.get(1).investigation == "running"  # 집었지만 끝내지 못함
    capsys.readouterr()

    monkeypatch.setattr(worker_module, "STALE_CLAIM", timedelta(0))
    llm = _scripted()
    cli.main(
        ["worker", "--once", "--config", _config(tmp_path), "--db", str(tmp_path / "kg.db")],
        make_reader=lambda cfg: FixtureReader(*raw_cluster("s04/localhost")),
        make_http=lambda cfg: FakeHttp(),
        make_llm=_llm_factory(llm),
    )

    assert llm.calls and store.get(1).investigation == "done"


def test_record_investigation_storage_error_is_mapped(tmp_path, capsys, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from core.db import InvestigationQueue

    def locked(*a, **k):
        raise OperationalError("update", {}, Exception("database is locked"))

    monkeypatch.setattr(InvestigationQueue, "complete", locked)

    code = _schedule_investigate(tmp_path, _llm_factory(_scripted()))

    assert code == 1 and "96001" in capsys.readouterr().err


# ── 알림 (#27) ──


def test_schedule_check_records_first_alert_in_dry_run(tmp_path, capsys):
    from core.db import AlertStore, create_db_engine

    cli.main(
        [*_args(tmp_path), "--trigger", "schedule"],
        make_reader=lambda cfg: FixtureReader(*raw_cluster("s01/detected")),
    )

    out = capsys.readouterr().out
    [m1] = AlertStore(create_db_engine(str(tmp_path / "kg.db"))).for_incident(1)
    assert (m1.kind, m1.dry_run) == ("M1", True)
    assert "알림: M1 (dry-run)" in out


def test_schedule_investigate_alerts_first_then_updates_with_cause(tmp_path):
    from core.db import AlertStore, create_db_engine

    _schedule_investigate(tmp_path, _llm_factory(_scripted()))

    kinds = [a.kind for a in AlertStore(create_db_engine(str(tmp_path / "kg.db"))).for_incident(1)]
    assert kinds == ["M1", "M1u", "M9"]  # 1차 감지는 조사 전에, 원인 갱신과 답글은 조사 뒤에


def test_unexpected_notify_error_does_not_stop_investigation(tmp_path):
    class Broken:
        dry_run = False

        def post(self, *a, **k):
            raise KeyError("ts")

        def update(self, *a, **k):
            raise KeyError("ts")

    llm = _scripted()
    code = cli.main(
        [*_investigate_args(tmp_path), "--trigger", "schedule"],
        make_reader=lambda cfg: FixtureReader(*raw_cluster("s04/localhost")),
        make_http=lambda cfg: FakeHttp(),
        make_llm=_llm_factory(llm),
        make_sender=lambda cfg: Broken(),
    )

    assert code == 0 and llm.calls


ASK_ANSWER = json.dumps({"sentences": [{"text": "이력 없음", "kind": "fact", "issue_ids": [1]}]})


def test_cli_ask_prints_answer(tmp_path, capsys):
    llm = HistoryLLM(script=["끝", ASK_ANSWER], calls=[], bound=[])
    code = cli.main(
        ["ask", "최근 이슈는?", "--config", _config(tmp_path), "--db", str(tmp_path / "kg.db")],
        make_llm=lambda _m: llm,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("대화 ")
    assert "[이슈 #" in out or "(추정)" in out


def test_cli_ask_rejects_bad_conversation_id(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["ask", "질문", "--conversation", "../x y", "--db", str(tmp_path / "kg.db")])
    assert e.value.code != 0
    assert "--conversation" in capsys.readouterr().err


# ── 작업자 (#28) ──


def test_worker_once_investigates_incident_queued_by_schedule_check(tmp_path, capsys):
    from core.db import IncidentStore, create_db_engine

    cli.main(
        [*_args(tmp_path), "--trigger", "schedule"],
        make_reader=lambda cfg: FixtureReader(*raw_cluster("s04/localhost")),
    )
    capsys.readouterr()

    code = cli.main(
        ["worker", "--once", "--config", _config(tmp_path), "--db", str(tmp_path / "kg.db")],
        make_reader=lambda cfg: FixtureReader(*raw_cluster("s04/localhost")),
        make_http=lambda cfg: FakeHttp(),
        make_llm=_llm_factory(_scripted()),
    )

    assert code == 0 and "이슈 #1 조사 끝" in capsys.readouterr().out
    assert IncidentStore(create_db_engine(str(tmp_path / "kg.db"))).get(1).investigation == "done"


def test_investigate_prints_judgement_and_llm_confidence(tmp_path, capsys):
    cli.main(
        _investigate_args(tmp_path),
        make_reader=lambda cfg: FixtureReader(*raw_cluster("s04/localhost")),
        make_http=lambda cfg: FakeHttp(),
        make_llm=_llm_factory(_scripted()),
    )

    out = capsys.readouterr().out
    assert "(판단 " in out and "LLM 확신도 high" in out and "판단 근거: " in out
