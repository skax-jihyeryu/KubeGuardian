"""확인하지 못한 기능 점검의 설정과 저장된 Service 스냅샷을 비교한다."""

from api.deps import get_config
from core.config import KubeGuardianConfig
from core.db import CheckRunStore
from service.model import ClusterSnapshot, Symptom
from tests.api.seed import at, finish_check, make_run


def _config(service="agnet"):
    return KubeGuardianConfig(
        project={"id": "dev"},
        target={"namespace": "dev"},
        core_paths={"chat": ["agent"]},
        api_scenarios=[
            {
                "name": "chat",
                "feature": "chat",
                "steps": [{"call": {"via": service, "path": "/chat"}}],
            }
        ],
    )


def _unknown(engine):
    run = make_run(engine)
    finish_check(
        engine,
        run,
        path="C",
        symptoms=[
            Symptom(scenario="chat", status="unknown", summary="대상 Service를 확인하지 못함")
        ],
    )
    return run


def test_missing_service_suggests_similar_snapshot_name(client, engine):
    client.app.dependency_overrides[get_config] = lambda: _config()
    run = _unknown(engine)
    CheckRunStore(engine).save_snapshot(
        run,
        ClusterSnapshot(
            namespace="dev",
            collected_at=at(0),
            resources={"Service": [{"metadata": {"name": "agent"}}]},
        ),
    )
    response = client.get(f"/dev/checks/{run}")
    assert response.status_code == 200
    assert "설정 확인과 수정 제안" in response.text
    assert "agnet" in response.text
    assert "비슷한 리소스" in response.text
    assert "agent" in response.text
    assert "자동으로 수정하지 않습니다" in response.text


def test_no_close_match_has_reason_without_candidate(client, engine):
    client.app.dependency_overrides[get_config] = lambda: _config("unrelated")
    run = _unknown(engine)
    CheckRunStore(engine).save_snapshot(
        run,
        ClusterSnapshot(
            namespace="dev",
            collected_at=at(0),
            resources={"Service": [{"metadata": {"name": "agent"}}]},
        ),
    )
    text = client.get(f"/dev/checks/{run}").text
    assert "비슷한 리소스가 없어 수정값을 제안할 수 없습니다" in text
    assert '<span class="k">비슷한 리소스</span>' not in text


def test_missing_snapshot_only_explains_limit(client, engine):
    client.app.dependency_overrides[get_config] = lambda: _config()
    run = _unknown(engine)
    text = client.get(f"/dev/checks/{run}").text
    assert "스냅샷이 없어 설정과 비교할 수 없습니다" in text


def test_actual_missing_scenario_service_shows_correction(client, engine):
    from service.pipeline.check import run_check
    from tests.service.checks.detectors.cases import NOW, FixtureReader, raw_cluster
    from tests.service.tools.fakes import FakeHttp

    config = _config("gatewayx")
    client.app.dependency_overrides[get_config] = lambda: config
    out = run_check(
        FixtureReader(*raw_cluster()), config, engine, trigger="check", now=NOW, http=FakeHttp()
    )

    text = client.get(f"/dev/checks/{out.run_id}").text
    assert "설정 확인과 수정 제안" in text
    assert "gatewayx" in text
    assert "비슷한 리소스" in text
    assert "gateway" in text
