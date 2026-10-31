"""run()의 증상 감지 '확인하지 못함' 무효 분기. 클러스터·쉘 스크립트 없이 가짜로 돈다."""

import json
import subprocess
from types import SimpleNamespace

import run as eval_run
from service.model import Symptom
from tests.service.checks.detectors.cases import CONFIG


def _patch_environment(monkeypatch, tmp_path, outcome):
    ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    monkeypatch.setattr(eval_run, "ROOT", tmp_path)
    monkeypatch.setattr(eval_run, "HISTORY", tmp_path / "history")
    monkeypatch.setattr(eval_run, "sh", lambda scenario, script: ok)
    monkeypatch.setattr(eval_run, "until_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(eval_run, "load_kubeguardian_config", lambda path: CONFIG)
    monkeypatch.setattr(eval_run.K8sClient, "from_config", staticmethod(lambda config: object()))
    monkeypatch.setattr(eval_run, "run_check", lambda *a, **k: outcome)


def _outcome(symptoms):
    return SimpleNamespace(
        run_id=1,
        signals=[],
        gate=SimpleNamespace(path="C", reason="test"),
        snapshot=SimpleNamespace(failed=[]),
        symptoms=symptoms,
    )


def _result(tmp_path):
    [path] = (tmp_path / "history").glob("*-H0/result.json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_unknown_symptom_makes_the_run_invalid(monkeypatch, tmp_path):
    unknown = Symptom(scenario="chat", status="unknown", summary="조회 실패: Service 목록")
    _patch_environment(monkeypatch, tmp_path, _outcome([unknown]))

    code = eval_run.run("H0", "unused.yaml", 1, 1, 1)

    result = _result(tmp_path)
    assert code == eval_run.EXIT_INVALID == result["exit_code"]
    assert result["invalid"].startswith("증상 감지 확인하지 못함")
    assert result["unknown_symptoms"] == ["chat: 조회 실패: Service 목록"]
    assert "score" not in result  # 채점하지 않는다


def test_no_unknown_symptom_is_scored_normally(monkeypatch, tmp_path):
    passed = Symptom(scenario="chat", status="passed", summary="모두 성공")
    _patch_environment(monkeypatch, tmp_path, _outcome([passed]))

    code = eval_run.run("H0", "unused.yaml", 1, 1, 1)

    result = _result(tmp_path)
    assert code == eval_run.EXIT_PASS
    assert "invalid" not in result and result["score"]["passed"]


def test_sh_survives_non_utf8_script_output(tmp_path, monkeypatch):
    scenario = tmp_path / "X1"
    scenario.mkdir()
    (scenario / "bad.sh").write_text("printf 'FAIL \\xea\\n' >&2; exit 1\n", encoding="utf-8")
    monkeypatch.setattr(eval_run, "SCENARIOS", tmp_path)

    r = eval_run.sh("X1", "bad.sh")

    assert r.returncode == 1
    assert "FAIL" in r.stderr
