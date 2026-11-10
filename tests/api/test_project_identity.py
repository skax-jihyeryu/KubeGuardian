"""프로젝트 식별자와 Kubernetes 네임스페이스를 서로 바꾸지 않는다."""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from core.config import KubeGuardianConfig, ProjectConfig, load_kubeguardian_config
from core.db import CheckRunStore
from deps import get_config
from service.chat import answer_question
from service.notify.notifier import Notifier
from tests.api.seed import at, finish_check, make_run
from tests.service.agent.history_qa.fakes import ScriptedLLM
from tests.service.notify.test_notifier import FakeSender, _sig
from web.viewmodels.shell import build_shell

CONFIG = KubeGuardianConfig(project={"id": "llm-gateway"}, target={"namespace": "dev"})


def test_default_project_is_explicitly_independent_of_namespace():
    assert KubeGuardianConfig(target={"namespace": "dev"}).project.id == "default"
    assert load_kubeguardian_config("kubeguardian.example.yaml").project.id == "llm-gateway"


@pytest.mark.parametrize("value", ["", "../dev", "Dev", "a/b", "a_b", "a" * 64, "-x"])
def test_project_id_must_be_dns_slug(value):
    with pytest.raises(ValidationError):
        ProjectConfig(id=value)


def test_routes_use_project_id_and_shell_displays_namespace(client, engine):
    client.app.dependency_overrides[get_config] = lambda: CONFIG
    run = make_run(engine)
    finish_check(engine, run, path="C")
    assert client.get("/").headers["location"] == "/llm-gateway/checks"
    for path in ["checks", f"checks/{run}", "issues", "setup/services", "chat"]:
        assert client.get(f"/llm-gateway/{path}").status_code == 200
        assert client.get(f"/dev/{path}").status_code == 404
    page = client.get("/llm-gateway/checks").text
    assert "llm-gateway · minikube" in page
    assert '<span class="wide"> · dev</span>' in page
    assert "네임스페이스 dev" not in page
    shell = build_shell(engine, CONFIG, at(30))
    assert shell.project == "llm-gateway" and shell.namespace == "dev"


def test_manual_check_keeps_namespace_when_project_differs(client):
    client.app.dependency_overrides[get_config] = lambda: CONFIG
    with patch("web.routes.checks.start_manual_check", return_value=42) as start:
        response = client.post("/llm-gateway/checks")
    assert response.headers["location"] == "/llm-gateway/checks/42"
    config = start.call_args.args[1]
    assert config.target.namespace == "dev"
    assert config.project.id == "llm-gateway"


def test_notification_links_use_project_id_and_keep_namespace_label(engine):
    from service.alerts.incidents import track
    from service.checks.gate import decide_gate

    run = make_run(engine)
    gate = decide_gate([_sig()])
    changes = track(
        engine,
        CONFIG,
        run_id=run,
        trigger="schedule",
        now=at(0),
        gate=gate,
        signals=[_sig()],
        symptoms=[],
    )
    sender = FakeSender()
    Notifier(engine, CONFIG, sender).after_check(run, gate, changes, at(0))
    text = sender.posts[0]["text"]
    assert f"http://localhost:8080/llm-gateway/issues/{changes[0].incident_id}" in text
    assert "/dev/" not in text
    assert "dev" in text


def test_chat_default_project_uses_config_for_latest_check_link(engine):
    import json

    run = CheckRunStore(engine).create("schedule", at(0))
    llm = ScriptedLLM(
        script=["끝", json.dumps({"sentences": [], "current_state": True})], calls=[], bound=[]
    )
    reply = answer_question(
        "지금 상태?", engine=engine, config=CONFIG, llm_factory=lambda _: llm, now=lambda: at(30)
    )
    assert f"/llm-gateway/checks/{run}" in reply.text
    assert "/dev/" not in reply.text


def test_reader_collects_dev_when_project_id_is_llm_gateway(engine):
    from core.db import CheckResultStore
    from service.notify.sender import DryRunSender
    from service.pipeline.manual_check import start_manual_check
    from tests.service.checks.detectors.cases import NOW, FixtureReader, raw_cluster

    namespaces = []
    tasks = []

    def reader(config):
        namespaces.append(config.target.namespace)
        return FixtureReader(*raw_cluster())

    run = start_manual_check(
        engine,
        CONFIG,
        NOW,
        tasks.append,
        reader_factory=reader,
        http_factory=lambda _: None,
        sender_factory=lambda _: DryRunSender(),
    )
    tasks[0]()
    assert namespaces == ["dev"]
    assert CheckResultStore(engine).get(run).status == "done"
    assert CheckRunStore(engine).get_snapshot(run).namespace == "dev"
