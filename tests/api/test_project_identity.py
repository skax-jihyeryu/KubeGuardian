"""프로젝트 식별자와 Kubernetes 네임스페이스를 서로 바꾸지 않는다."""

import pytest
from pydantic import ValidationError

from api.deps import get_config
from api.viewmodels.shell import build_shell
from core.config import KubeGuardianConfig, ProjectConfig, load_kubeguardian_config
from core.db import CheckRunStore
from service.chat import answer_question
from tests.api.seed import at, finish_check, make_run
from tests.service.agent.history_qa.fakes import ScriptedLLM

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
    for path in ["checks", f"checks/{run}"]:
        assert client.get(f"/llm-gateway/{path}").status_code == 200
        assert client.get(f"/dev/{path}").status_code == 404
    page = client.get("/llm-gateway/checks").text
    assert "llm-gateway · minikube" in page
    assert "네임스페이스 dev" in page
    shell = build_shell(engine, CONFIG, at(30))
    assert shell.project == "llm-gateway" and shell.namespace == "dev"


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
