"""조사 지침 작성 규칙 (runbooks/_template.md 주석)을 테스트로 고정한다."""

import re
from pathlib import Path

import pytest
import tiktoken

from service.checks.detectors.catalog import CATALOG

RUNBOOKS = Path(__file__).parents[3] / "app" / "service" / "knowledge" / "runbooks"
FILES = sorted(p for p in RUNBOOKS.glob("*.md") if not p.name.startswith("_"))
# 환경 설명에만 두는 진단 대상 고유 이름·경로·env.
# 한국어 조사가 붙어도 잡도록 \\b 대신 영숫자 경계를 쓴다
_NAMES = "gateway|llm-gateway|front-chat|front-admin|admin|agent|redis|postgres|dev-account"
ENV_NAMES = re.compile(
    rf"(?<![A-Za-z0-9_-])({_NAMES})(?![A-Za-z0-9_-])|/api/v\d|[A-Z]+_SERVICE_URL|-svc\b"
)
SECTIONS = [
    "## 한 줄 설명",
    "## 진입 조건",
    "## 먼저 확인할 사실",
    "## 흔한 함정",
    "## 확정하지 못할 때",
    "## 권장 조치 방향",
    "## 출처",
]
# noise-filtering은 원인을 찾는 지침이 아니라 판단 요령이라 원인 후보 대신 판단 요령 섹션을 둔다
CAUSE_SECTION = {"noise-filtering": "## 판단 요령"}


def test_every_runbook_referenced_by_rules_exists():
    referenced = {r for rule in CATALOG.values() for r in rule.runbooks}

    assert referenced <= {p.stem for p in FILES}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_runbook_is_at_most_1000_tokens(path):
    assert len(tiktoken.get_encoding("o200k_base").encode(path.read_text())) <= 1000


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_runbook_has_no_environment_specific_names(path):
    assert ENV_NAMES.findall(path.read_text()) == []


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_runbook_follows_template_sections(path):
    text = path.read_text()

    assert [s for s in SECTIONS if s not in text] == []
    assert CAUSE_SECTION.get(path.stem, "## 원인 후보와 판별법") in text


def test_environment_name_pattern_catches_korean_particles():
    assert ENV_NAMES.findall("gateway가 redis를 부른다")
