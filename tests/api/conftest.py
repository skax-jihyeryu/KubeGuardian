import pytest
from fastapi.testclient import TestClient

from api.deps import get_config, get_engine, get_now
from core.config import KubeGuardianConfig
from core.db import create_db_engine
from main import create_app
from tests.api.seed import at


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(tmp_path / "kg.db")


class Clock:
    """화면이 보는 '지금'. 테스트가 옮긴다."""

    def __init__(self):
        self.now = at(21)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(engine, clock):
    app = create_app()
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_config] = lambda: KubeGuardianConfig(
        target={"namespace": "dev"}, core_paths={"chat": ["agent"]}
    )
    app.dependency_overrides[get_now] = lambda: clock.now
    return TestClient(app, follow_redirects=False)
