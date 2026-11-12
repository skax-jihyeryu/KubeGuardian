from datetime import UTC, datetime

from run import alert_timing


def test_alert_timing_uses_persisted_time_and_only_accepted_cause_on_same_incident():
    injected = datetime(2026, 1, 1, tzinfo=UTC)
    detection = {"id": 1, "incident_id": 4, "kind": "M1", "sent_at": "2026-01-01T00:00:02+00:00"}
    update = detection | {"id": 2, "kind": "M1u", "sent_at": "2026-01-01T00:00:08+00:00"}
    accepted = {
        "cause_alert": update,
        "passed": True,
        "status": "done",
        "judgement": "confirmed",
        "conclusion_supported": True,
    }
    earlier = update | {"sent_at": "2026-01-01T00:00:05+00:00"}
    runs = [
        accepted | {"judgement": "guess", "cause_alert": earlier},
        accepted | {"cause_alert": earlier | {"incident_id": 9}},
        accepted,
    ]

    timing = alert_timing(injected, [{"detection_alert": detection}], runs)

    assert timing["detection"] == {"status": "recorded", "elapsed_s": 2, "alert": detection}
    assert timing["cause"] == {"status": "recorded", "elapsed_s": 8, "alert": update}


def test_alert_timing_distinguishes_missing_legacy_and_negative_wall_time():
    injected = datetime(2026, 1, 1, tzinfo=UTC)
    assert alert_timing(injected, [{}], [])["detection"]["status"] == "unsupported"
    timing = alert_timing(injected, [{"detection_alert": None}], [])
    assert timing["detection"]["status"] == timing["cause"]["status"] == "missing"

    alert = {"id": 1, "incident_id": 1, "kind": "M1", "sent_at": "2025-12-31T23:59:59+00:00"}
    timing = alert_timing(injected, [{"detection_alert": alert}], [])
    assert timing["detection"] == {"status": "unsupported", "elapsed_s": None}


def test_local_eval_reports_the_detection_recorded_in_its_db(tmp_path, monkeypatch):
    import json
    import subprocess

    import run as eval_run
    from core.db import AlertStore, create_db_engine
    from tests.service.checks.detectors.cases import CONFIG, FixtureReader, raw_cluster

    scenario = tmp_path / "scenarios" / "X1"
    scenario.mkdir(parents=True)
    (scenario / "expected.yaml").write_text(
        "id: X1\ngate_path: A\nmust_signals: [S01]\n", encoding="utf-8"
    )
    history = tmp_path / "history"
    monkeypatch.setattr(eval_run, "ROOT", tmp_path)
    monkeypatch.setattr(eval_run, "SCENARIOS", scenario.parent)
    monkeypatch.setattr(eval_run, "HISTORY", history)
    monkeypatch.setattr(eval_run, "sh", lambda *args: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(eval_run, "until_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(eval_run, "load_kubeguardian_config", lambda path: CONFIG)
    monkeypatch.setattr(eval_run, "target_http", lambda config: None)
    monkeypatch.setattr(
        eval_run.K8sClient,
        "from_config",
        lambda config: FixtureReader(*raw_cluster("s01/detected")),
    )

    assert eval_run.run("X1", "unused.yaml", 1, 1, 1) == eval_run.EXIT_PASS

    [path] = history.glob("*/result.json")
    result = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(result["first_detected_s"]["S01"], int)
    detection = result["alert_timing"]["detection"]
    [row] = AlertStore(create_db_engine(path.parent / "kg.db")).list_alerts()
    assert row.dry_run and row.kind == "M1"
    assert detection["status"] == "recorded"
    assert detection["alert"]["sent_at"] == row.sent_at.isoformat()
    assert detection["alert"]["id"] == row.id
    assert result["alert_timing"]["cause"]["status"] == "missing"


def test_alert_timing_treats_unusable_protocol_timestamps_as_unsupported():
    injected = datetime(2026, 1, 1, tzinfo=UTC)
    detection = {"id": 1, "incident_id": 4, "kind": "M1", "sent_at": "2026-01-01T00:00:02"}

    timing = alert_timing(injected, [{"detection_alert": detection}], [])

    assert timing["detection"] == {"status": "unsupported", "elapsed_s": None}


def test_alert_timing_uses_valid_cause_when_an_earlier_record_predates_detection():
    injected = datetime(2026, 1, 1, tzinfo=UTC)
    detection = {"id": 1, "incident_id": 4, "kind": "M1", "sent_at": "2026-01-01T00:00:02+00:00"}
    stale = detection | {"id": 2, "kind": "M1u", "sent_at": "2026-01-01T00:00:01+00:00"}
    valid = stale | {"id": 3, "sent_at": "2026-01-01T00:00:07+00:00"}
    accepted = {
        "passed": True,
        "status": "done",
        "judgement": "confirmed",
        "conclusion_supported": True,
    }
    runs = [accepted | {"cause_alert": a} for a in (stale, valid)]

    timing = alert_timing(injected, [{"detection_alert": detection}], runs)

    assert timing["cause"] == {"status": "recorded", "elapsed_s": 7, "alert": valid}
