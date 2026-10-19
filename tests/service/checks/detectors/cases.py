"""감지기 테스트 공용: JSON 픽스처로 스냅샷을 만들고 감지기를 돌린다 (05-rules §7).

픽스처는 Kubernetes API 응답의 items 모양(원본 JSON)이고 수집기(collect)로 정규화한다.
- `tests/fixtures/cluster/healthy.json`: 정상 기준 클러스터
- `tests/fixtures/detectors/<규칙>/<경우>.json`: 기준에서 바꾼 부분만 적는다
  - `merge`: 같은 이름 객체에 깊게 합친다 (dict는 합치고 list·값은 바꾼다, null은 키를 지운다)
  - `add`: 새 객체, `delete`: 지울 객체 이름, `failed`: 수집 실패로 둘 kind → 사유
"""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.config import KubeGuardianConfig
from core.k8s import K8sCallError
from service.checks.detectors.base import DetectorContext, Observation, registered
from service.checks.runner import load_detectors
from service.model import ClusterSnapshot, Signal
from service.sensors.collector import collect

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
NOW = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
CONFIG = KubeGuardianConfig.model_validate(
    {
        "target": {"namespace": "dev"},
        "core_paths": {"chat": ["gateway", "agent"]},
        "external_hosts": ["*.openai.azure.com"],
    }
)


class MemoryObservations:
    """ObservationStore 메모리 구현 (SQLite 구현과 같은 의미)."""

    def __init__(self):
        self.rows: dict[tuple[str, str], Observation] = {}

    def get(self, rule_id, subject):
        return self.rows.get((rule_id, subject))

    def touch(self, rule_id, subject, now, values=None):
        old = self.rows.get((rule_id, subject))
        obs = Observation(
            first_seen=old.first_seen if old else now,
            last_seen=now,
            values=values if values is not None else (old.values if old else {}),
        )
        self.rows[(rule_id, subject)] = obs
        return obs

    def clear(self, rule_id, subject):
        self.rows.pop((rule_id, subject), None)

    def prune(self, rule_id, keep_subjects):
        for key in [k for k in self.rows if k[0] == rule_id and k[1] not in keep_subjects]:
            del self.rows[key]

    def seed(self, rule_id, subject, first_seen, values=None):
        self.rows[(rule_id, subject)] = Observation(first_seen, first_seen, values or {})


def _deep_merge(base: Any, patch: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = dict(base)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = _deep_merge(base.get(k), v)
    return out


def raw_cluster(case: str | None = None) -> tuple[dict[str, list[dict]], dict[str, str]]:
    base = json.loads((FIXTURES / "cluster" / "healthy.json").read_text(encoding="utf-8"))
    base.pop("_description", None)
    failed: dict[str, str] = {}
    if case:
        spec = json.loads((FIXTURES / "detectors" / f"{case}.json").read_text(encoding="utf-8"))
        for kind, patches in spec.get("merge", {}).items():
            for patch in patches:
                name = patch["metadata"]["name"]
                idx = next(i for i, o in enumerate(base[kind]) if o["metadata"]["name"] == name)
                base[kind][idx] = _deep_merge(base[kind][idx], patch)
        for kind, objs in spec.get("add", {}).items():
            base.setdefault(kind, []).extend(objs)
        for kind, names in spec.get("delete", {}).items():
            base[kind] = [o for o in base[kind] if o["metadata"]["name"] not in names]
        failed = spec.get("failed", {})
    return base, failed


class FixtureReader:
    def __init__(self, objects, failed):
        self.namespace = "dev"
        self._objects, self._failed = objects, failed

    def list(self, kind):
        if kind in self._failed:
            raise K8sCallError(self._failed[kind])
        return copy.deepcopy(self._objects.get(kind, []))

    def list_nodes(self):
        return self.list("Node")

    def read_log(self, pod, container, *, previous, tail_lines):  # pragma: no cover
        return ""


def snapshot(case: str | None = None, now: datetime = NOW) -> ClusterSnapshot:
    return collect(FixtureReader(*raw_cluster(case)), now=now)


def failed_snapshot(*kinds: str, now: datetime = NOW) -> ClusterSnapshot:
    objects, _ = raw_cluster()
    return collect(FixtureReader(objects, {k: "403 Forbidden" for k in kinds}), now=now)


def context(snap, *, previous=None, observations=None, now=NOW, config=CONFIG):
    return DetectorContext(
        snapshot=snap,
        previous=previous,
        observations=observations if observations is not None else MemoryObservations(),
        config=config,
        now=now,
    )


def run(rule_id: str, ctx: DetectorContext) -> list[Signal]:
    load_detectors()
    return registered()[rule_id](ctx)


def one(signals: list[Signal], subject: str) -> Signal:
    matches = [s for s in signals if s.subject == subject]
    assert len(matches) == 1, [s.subject for s in signals]
    return matches[0]
