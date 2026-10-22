"""Pod 안 진입점(runner_step) 단위 테스트. 가짜 클러스터·LLM으로 돈다."""

import json

from langchain_core.messages import AIMessage

import runner_step
from tests.service.agent.investigator.test_v1_graph import ScriptedLLM
from tests.service.checks.detectors.cases import FixtureReader, raw_cluster

CONFIG_YAML = """
target: {namespace: dev}
core_paths: {chat: [gateway, agent]}
"""


def _config(tmp_path):
    path = tmp_path / "kubeguardian.yaml"
    path.write_text(CONFIG_YAML, encoding="utf-8")
    return str(path)


def _reader(case):
    return lambda cfg: FixtureReader(*raw_cluster(case))


def _check(tmp_path, capsys, case, run="r1"):
    db = tmp_path / run / "kg.db"
    code = runner_step.main(
        ["check", "--config", _config(tmp_path), "--db", str(db), "--json"],
        make_reader=_reader(case),
    )
    out = capsys.readouterr().out
    return code, out, db


def test_check_prints_one_json_line_with_outcome(tmp_path, capsys):
    code, out, _ = _check(tmp_path, capsys, "s01/detected")

    assert code == 0
    assert len(out.strip().splitlines()) == 1
    data = json.loads(out)
    assert data["gate_path"] == "A"
    assert data["run_id"] == 1
    assert any(s["rule_id"] == "S01" and s["verdict"] == "detected" for s in data["signals"])
    assert data["snapshot_failed"] == {}


def test_check_reports_collection_failure_in_same_shape(tmp_path, capsys):
    objects, _ = raw_cluster()
    code = runner_step.main(
        ["check", "--config", _config(tmp_path), "--db", str(tmp_path / "kg.db"), "--json"],
        make_reader=lambda cfg: FixtureReader(objects, {"Secret": "403 Forbidden"}),
    )

    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["snapshot_failed"] == {"Secret": "403 Forbidden"}
    assert data["gate_path"] == "C"


def test_repeated_checks_continue_in_the_same_db(tmp_path, capsys):
    """반복 점검은 같은 DB를 이어 써야 지속 시간 규칙이 앞 점검의 관찰 기록을 본다."""
    _, first, db = _check(tmp_path, capsys, "s02/partial")
    _, second, db2 = _check(tmp_path, capsys, "s02/partial")

    assert db == db2
    assert json.loads(first)["run_id"] == 1
    assert json.loads(second)["run_id"] == 2


def test_investigate_rebuilds_outcome_from_the_check_and_prints_result(tmp_path, capsys):
    _, out, db = _check(tmp_path, capsys, "s04/localhost")
    check = json.loads(out)
    s04 = next(s for s in check["signals"] if s["rule_id"] == "S04" and s["evidence_id"])
    triage = json.dumps(
        {
            "issues": [
                {
                    "rank": 1,
                    "title": "주소 설정 오류",
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
            "evidence_ids": [s04["evidence_id"]],
        }
    )
    llm = ScriptedLLM(script=[triage, AIMessage(content="조사 완료"), report], calls=[])

    code = runner_step.main(
        [
            "investigate",
            "--config",
            _config(tmp_path),
            "--db",
            str(db),
            "--check-run-id",
            str(check["run_id"]),
            "--run-id",
            "eval-x",
            "--json",
        ],
        make_reader=_reader("s04/localhost"),
        llm_factory=lambda m: llm,
    )

    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["status"] == "done", data
    assert data["report"]["conclusion"] == "gateway 주소 env가 localhost"
    assert data["usage"]["llm_calls"] >= 1
    assert {"steps", "trace_id"} <= data.keys()


def test_investigate_on_gate_c_prints_null_result(tmp_path, capsys):
    _, out, db = _check(tmp_path, capsys, None)

    code = runner_step.main(
        [
            "investigate",
            "--config",
            _config(tmp_path),
            "--db",
            str(db),
            "--check-run-id",
            "1",
            "--run-id",
            "eval-x",
            "--json",
        ],
        make_reader=_reader(None),
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"skipped": True}


def test_investigate_without_saved_outcome_fails(tmp_path, capsys):
    code = runner_step.main(
        [
            "investigate",
            "--config",
            _config(tmp_path),
            "--db",
            str(tmp_path / "x" / "kg.db"),
            "--check-run-id",
            "7",
            "--run-id",
            "eval-x",
            "--json",
        ],
        make_reader=_reader(None),
    )

    assert code == 2
    assert "outcome" in capsys.readouterr().err
