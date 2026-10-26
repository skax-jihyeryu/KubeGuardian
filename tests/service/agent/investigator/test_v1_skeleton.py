from service.agent import registry
from service.agent.investigator.v1.agent import InvestigatorV1
from service.agent.investigator.v1.config import SPEC


def test_investigator_v1_is_registered_with_role_models():
    assert registry.get("investigator:v1") is InvestigatorV1
    assert set(SPEC.models) == {"triage", "plan", "investigate", "report"}
