import cli
from core.error.service_exception import ErrorCode, ServiceException
from tests.service.checks.detectors.cases import FixtureReader, raw_cluster
from tests.service.tools.fakes import FakeHttp

CONFIG_YAML = """
target: {namespace: dev}
core_paths: {chat: [gateway, agent]}
"""


def _config(tmp_path):
    path = tmp_path / "kubeguardian.yaml"
    path.write_text(CONFIG_YAML, encoding="utf-8")
    return str(path)


SCENARIO_YAML = (
    CONFIG_YAML
    + """
api_scenarios:
  - name: chat
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
