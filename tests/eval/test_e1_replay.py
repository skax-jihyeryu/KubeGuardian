from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from service.agent._common.tool_adapter import to_langchain_tool
from service.model.report import Issue, Report, ResourceRef
from service.tools.base import ToolContext
from service.tools.resource import GetResource

SPEC = spec_from_file_location(
    "e1_replay", Path(__file__).resolve().parents[2] / "scenarios/E1/replay.py"
)
e1 = module_from_spec(SPEC)
SPEC.loader.exec_module(e1)


def test_old_restart_stays_clear_and_is_visible_to_investigator_tool():
    clean, decoy = e1.Replay(False), e1.Replay(True)
    clean_status = clean.pod_status(e1.DECOY)
    status = decoy.pod_status(e1.DECOY)
    assert clean_status["restartCount"] == 0
    assert status["restartCount"] == 3
    assert status["ready"]
    assert status["lastState"]["terminated"]["finishedAt"] < e1.NOW.isoformat()
    assert clean.p06(e1.DECOY).verdict == "passed"
    assert decoy.p06(e1.DECOY).verdict == "passed"
    assert decoy.p06(e1.DECOY).values["previous_restartCount"] == 3
    status["restartCount"] = 0
    status.pop("lastState")
    assert decoy.data == clean.data


def test_production_resource_tool_keeps_replay_scope():
    replay = e1.Replay(True)
    evidence = e1.Evidence()
    ctx = ToolContext(namespace=replay.namespace, k8s=replay, evidence=evidence)
    resource = to_langchain_tool(GetResource(e1.CONFIG), ctx)
    bad_scope = resource.invoke({"kind": "Pod", "namespace": "other"})
    assert "VALIDATION_ERROR" in bad_scope
    assert not evidence.saved
    scoped = resource.invoke({"kind": "Pod", "label_selector": "app=identity"})
    assert "identity-v1-pod" in scoped
    assert e1.DECOY not in scoped
    assert all(item.subject.startswith("replay/") for item in evidence.saved.values())


def test_rank_one_and_conclusion_both_count():
    report = Report(
        conclusion="The identity service failed writing a session to sessionstore",
        confidence="medium",
        impact_scope="Login",
        issues=[
            Issue(
                rank=1,
                title="Identity connection timeout writing to sessionstore",
                impact="core_path_broken",
                confidence="medium",
                cause_resource=ResourceRef(kind="Deployment", name="identity"),
            )
        ],
    )
    assert e1.score(report)["top1_expected"]
    assert e1.score(report)["session_timeout_mentioned"]
    assert not e1.score(report)["decoy_claims_for_review"]
    report.conclusion = "The recovered thumbnailer restart did not cause the login timeout."
    assert e1.score(report)["decoy_claims_for_review"] == [report.conclusion]
    assert not e1.score(report)["decoy_rank1_resource"]
    report.conclusion = "A thumbnailer restart caused the login timeout."
    assert e1.score(report)["decoy_claims_for_review"] == [report.conclusion]
    report.issues[0].cause_resource.name = e1.DECOY
    assert not e1.score(report)["top1_expected"]
    assert e1.score(report)["decoy_rank1_resource"]


def test_scripted_model_sees_same_login_failure_and_only_variant_has_restart():
    import json

    from langchain_core.messages import AIMessage

    from tests.service.agent.investigator.test_v1_graph import ScriptedLLM

    prompts = []
    for decoy in (False, True):
        llm = ScriptedLLM(
            script=[
                json.dumps(
                    {
                        "issues": [
                            {
                                "rank": 1,
                                "title": "Login fails",
                                "impact": "core_path_broken",
                                "confidence": "medium",
                                "evidence_ids": [],
                            }
                        ],
                        "runbook": "service-unreachable",
                    }
                ),
                json.dumps({"hypotheses": [], "order": ["run", "wiring", "logs", "config"]}),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_resource",
                            "args": {"kind": "Pod"},
                            "id": "inspect-pod",
                        }
                    ],
                ),
                "Enough evidence to report.",
                json.dumps(
                    {
                        "conclusion": "Login timed out",
                        "impact_scope": "Login",
                        "confidence": "low",
                        "evidence_ids": ["ev-0001"],
                    }
                ),
            ]
            + [
                "No additional observations.",
                json.dumps(
                    {
                        "conclusion": "Login timed out",
                        "impact_scope": "Login",
                        "confidence": "low",
                        "evidence_ids": ["ev-0001"],
                    }
                ),
            ],
        )
        output = e1.run(decoy, lambda _, llm=llm: llm)
        assert output["result"]["status"] in ("done", "incomplete"), output["result"]["error"]
        assert output["gate"] == "B"
        assert output["decoy_observed"]
        prompt = "\n".join(str(message.content) for call in llm.calls for message in call)
        assert "login /signin → 504" in prompt
        assert "가상 환경의 로그인 장애" in prompt
        assert "SESSIONSTORE_URL" not in prompt  # Env values are resolved by the topology tool.
        prompts.append(prompt)
        assert "sessionstore:6379" in prompt
        assert ("2026-01-01T09:00:01Z" in prompt) is decoy

    # The scripted graph makes the same calls. Only the Pod's old restart differs in its prompts.
    clean, decoy = prompts
    normalized = decoy.replace('"restartCount":3', '"restartCount":0').replace(
        ',"lastTerminated":{"reason":"OOMKilled","exitCode":137,"finishedAt":"2026-01-01T09:00:01Z"}',
        "",
    )
    assert normalized == clean
