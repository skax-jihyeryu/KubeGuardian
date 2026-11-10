"""점검 구성은 정의만 보여 주고 규칙 선택만 저장한다."""

import pytest

from core.config import KubeGuardianConfig
from core.db import CheckResultStore, RuleSelectionStore
from deps import get_config
from service.checks.detectors.catalog import CATALOG
from service.model import Signal
from tests.api.seed import at, make_run


@pytest.fixture
def setup_client(client):
    return client


def test_setup_redirect_and_project(setup_client):
    response = setup_client.get("/dev/setup")
    assert response.status_code == 303
    assert response.headers["location"] == "/dev/setup/services"
    assert setup_client.get("/other/setup/rules").status_code == 404


def test_services_definition_and_masking(setup_client):
    setup_client.app.dependency_overrides[get_config] = lambda: KubeGuardianConfig(
        project={"id": "dev"},
        target={"namespace": "dev"},
        core_paths={"chat": ["agent"]},
        required_config={"agent": ["API_KEY"]},
        app_apis={"agent": ["/health?token=secret-value"]},
    )
    response = setup_client.get("/dev/setup/services")
    assert response.status_code == 200
    assert "API_KEY" in response.text
    assert "읽기 전용" in response.text
    assert "secret-value" not in response.text
    assert setup_client.get("/dev/setup/services?service=missing").status_code == 404


def test_rules_groups_and_candidates(setup_client):
    response = setup_client.get("/dev/setup/rules")
    assert response.status_code == 200
    assert "준비 중" not in response.text
    assert "사용 14 · 끔 0 · 향후 계획 11" in response.text
    main, fold = response.text.split('<details class="future"', 1)
    assert "N02" not in main
    assert "N02" in fold
    assert 'value="N02"' not in response.text
    assert '<details class="future" open' not in response.text
    assert "향후 계획 11개" in fold
    assert "향후 계획 2개" in main
    assert 'value="C05" aria-label="C05 점검 사용" checked' in response.text
    assert "점검 중단 감시 · 구현 완료" in response.text
    response = setup_client.get("/dev/setup/rules?view=kind&rule=S03")
    assert "Service" in response.text
    assert "targetPort 불일치" in response.text
    main, fold = response.text.split('<details class="future"', 1)
    assert "P01" not in main
    assert "P01" in fold
    assert setup_client.get("/dev/setup/rules?view=bad").status_code == 400
    assert setup_client.get("/dev/setup/rules?rule=bad").status_code == 404


def test_future_rule_selected_opens_fold_and_shows_plan(setup_client):
    text = setup_client.get("/dev/setup/rules?rule=N02").text
    assert '<details class="future" open>' in text
    assert "<h2>향후 계획</h2>" in text
    assert "다음 단계에서 구현할 규칙입니다" in text
    assert "준비 중" not in text


def test_selection_save_and_reject_candidate(setup_client, engine):
    response = setup_client.post("/dev/setup/rules", data={"enabled": "W02", "view": "kind"})
    assert response.status_code == 303
    assert "view=kind&saved=1" in response.headers["location"]
    assert RuleSelectionStore(engine).disabled() == set(CATALOG) - {"W02"}
    assert setup_client.post("/dev/setup/rules", data={"enabled": "N02"}).status_code == 400
    assert RuleSelectionStore(engine).disabled() == set(CATALOG) - {"W02"}
    setup_client.post("/dev/setup/rules", data={})
    assert RuleSelectionStore(engine).disabled() == set(CATALOG)


def test_recent_detected_links(setup_client, engine):
    run = make_run(engine)
    store = CheckResultStore(engine)
    store.start(run, at(0))
    store.finish(
        run,
        "done",
        at(1),
        None,
        [
            Signal(
                rule_id="W02",
                verdict="detected",
                subject="dev/Deployment/agent",
                severity="warning",
            ),
        ],
        [],
        rules=["W02"],
    )
    response = setup_client.get("/dev/setup/rules?rule=W02")
    assert f'href="/dev/checks/{run}"' in response.text


def test_features_empty_and_yaml_escaped(setup_client):
    assert "아직 등록한 기능 점검이 없습니다" in setup_client.get("/dev/setup/features").text
    setup_client.app.dependency_overrides[get_config] = lambda: KubeGuardianConfig(
        project={"id": "dev"},
        target={"namespace": "dev"},
        core_paths={"chat": ["agent"]},
        api_scenarios=[
            {
                "name": "chat",
                "feature": "chat",
                "steps": [
                    {
                        "call": {
                            "via": "agent",
                            "path": "/chat",
                            "body": {
                                "message": "<script>alert(1)</script>",
                                "password": "secret-value",
                            },
                        }
                    }
                ],
            }
        ],
    )
    response = setup_client.get("/dev/setup/features?feature=chat")
    assert response.status_code == 200
    assert "&lt;script&gt;" in response.text
    assert "secret-value" not in response.text
    assert setup_client.get("/dev/setup/features?feature=missing").status_code == 404


def test_active_rule_conditions_and_sources(setup_client):
    from web.viewmodels.setup import RULE_DETAILS

    assert set(RULE_DETAILS) == set(CATALOG)
    for rule, (predicate, source, _) in RULE_DETAILS.items():
        response = setup_client.get(f"/dev/setup/rules?rule={rule}")
        assert response.status_code == 200
        assert predicate in response.text
        assert source in response.text
    text = setup_client.get("/dev/setup/rules?rule=W02").text
    assert "ProgressDeadlineExceeded" in text
    assert 'href="https://kubernetes.io/docs/concepts/workloads/controllers/deployment/"' in text


def test_rule_post_invalid_utf8_is_bad_request(setup_client, engine):
    response = setup_client.post("/dev/setup/rules", content=b"enabled=\xff")
    assert response.status_code == 400
    assert RuleSelectionStore(engine).disabled() == set()


def test_recent_detected_link_survives_many_newer_calm_runs(setup_client, engine):
    from sqlalchemy.orm import Session

    from core.db.tables import CheckResultRow

    run = make_run(engine)
    store = CheckResultStore(engine)
    store.start(run, at(0))
    store.finish(
        run,
        "done",
        at(1),
        None,
        [
            Signal(
                rule_id="W02",
                verdict="detected",
                subject="dev/Deployment/agent",
                severity="warning",
            ),
        ],
        [],
        rules=["W02"],
    )
    with Session(engine) as session, session.begin():
        for _ in range(101):
            newer = make_run(engine)
            session.add(
                CheckResultRow(
                    run_id=newer, status="done", started_at=at(2), verdicts=[], symptoms=[]
                )
            )
    text = setup_client.get("/dev/setup/rules?rule=W02").text
    assert f'href="/dev/checks/{run}"' in text


def test_service_shows_authenticated_api_reference_without_credentials(setup_client):
    setup_client.app.dependency_overrides[get_config] = lambda: KubeGuardianConfig(
        project={"id": "dev"},
        target={"namespace": "dev"},
        app_apis={"gateway": ["/models/${org_id}"]},
        app_api_auth={
            "gateway": {
                "via": "gateway",
                "path": "/api/v1/auth/login",
                "account_secret": "kg-test-user",
                "token": "access_token",
                "extract": {"org_id": "user.role.organizations.0.org_id"},
            }
        },
    )
    response = setup_client.get("/dev/setup/services?service=gateway")
    assert response.status_code == 200
    assert "로그인 인증" in response.text
    assert "kg-test-user" in response.text
    assert "/api/v1/auth/login" in response.text
    assert "/models/${org_id}" in response.text
    assert "Authorization" not in response.text


def test_rule_details_show_catalog_input_fields(setup_client):
    from service.checks.detectors.catalog import CATALOG

    html = setup_client.get("/dev/setup/rules?rule=S04").text
    assert "입력 정보" in html
    for kind, fields in CATALOG["S04"].inputs:
        assert kind in html
        assert fields in html
