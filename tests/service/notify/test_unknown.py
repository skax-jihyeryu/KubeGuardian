from core.config import KubeGuardianConfig
from core.db import AlertStore, create_db_engine
from service.model import GateResult, Signal, Symptom
from service.notify.notifier import Notifier
from tests.api.seed import at, finish_check, make_run
from tests.service.notify.test_notifier import FakeSender


def test_unknown_threshold_once_and_recovery_resets(tmp_path):
    engine = create_db_engine(tmp_path / "kg.db")
    sender = FakeSender()
    config = KubeGuardianConfig(project={"id": "dev"}, target={"namespace": "dev"})
    gate = GateResult(path="C", reason="unknown")

    def check(index, unknown=True):
        run = make_run(engine, start=at(index * 300))
        signals = [
            Signal(
                rule_id="S01",
                verdict="unknown" if unknown else "passed",
                subject="dev/Service/agent",
                message="수집 실패",
            )
        ]
        finish_check(engine, run, path="C", signals=signals, start=at(index * 300))
        notifier = Notifier(engine, config, sender)
        notifier.after_check(run, gate, [], at(index * 300 + 12))
        return run

    check(0)
    check(1)
    assert not sender.posts
    third = check(2)
    assert len(sender.posts) == 1
    assert f"/checks/{third}#s-judge" in sender.posts[0]["text"]
    Notifier(engine, config, sender).after_check(third, gate, [], at(620))
    check(3)
    assert len(sender.posts) == 1
    check(4, unknown=False)
    check(5)
    check(6)
    check(7)
    assert len(sender.posts) == 2
    assert len(AlertStore(engine).list_alerts()) == 2


def test_unknown_failed_alert_retries_during_same_streak(tmp_path):
    engine = create_db_engine(tmp_path / "kg.db")
    sender = FakeSender(fail=True)
    config = KubeGuardianConfig(project={"id": "dev"}, target={"namespace": "dev"})
    gate = GateResult(path="C", reason="unknown")
    for index in range(5):
        run = make_run(engine, start=at(index * 300))
        finish_check(
            engine,
            run,
            path="C",
            start=at(index * 300),
            signals=[Signal(rule_id="S01", verdict="unknown", subject="dev/Service/agent")],
        )
        if index == 3:
            sender.fail = False
        Notifier(engine, config, sender).after_check(run, gate, [], at(index * 300 + 12))
    assert len(sender.posts) == 1
    alerts = AlertStore(engine).list_alerts()
    assert len(alerts) == 2
    assert sum(a.error is None for a in alerts) == 1


def test_unknown_symptom_without_failed_step_has_no_none(tmp_path):
    engine = create_db_engine(tmp_path / "kg.db")
    sender = FakeSender()
    config = KubeGuardianConfig(project={"id": "dev"}, target={"namespace": "dev"})
    gate = GateResult(path="C", reason="unknown")
    for index in range(3):
        run = make_run(engine, start=at(index * 300))
        finish_check(
            engine,
            run,
            path="C",
            signals=[],
            symptoms=[Symptom(scenario="chat", status="unknown", summary="계정 없음")],
            start=at(index * 300),
        )
        Notifier(engine, config, sender).after_check(run, gate, [], at(index * 300 + 12))
    assert len(sender.posts) == 1
    assert "None" not in sender.posts[0]["text"]
    assert "• 기능 점검 chat\n" in sender.posts[0]["text"]
