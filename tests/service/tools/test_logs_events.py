import pytest

from core.error.service_exception import ServiceException
from core.k8s import K8sCallError
from service.tools.events import GetEvents
from service.tools.logs import GetPodLogs
from tests.service.tools.fakes import FakeK8s, event, pod

LOG = "\n".join(
    [
        "2026-10-20T00:01:00Z INFO start",
        "2026-10-20T00:02:00Z ERROR db password=hunter2 refused",
        "2026-10-20T00:03:00Z ERROR retry",
        "no timestamp line",
    ]
)

# ── get_pod_logs ──


def _k8s(logs=None):
    return FakeK8s({"Pod": [pod("gateway-1")]}, logs=logs or {("gateway-1", "gateway", False): LOG})


def test_pod_logs_returns_redacted_lines_and_time_range(make_ctx, evidence):
    k8s = _k8s()

    result = GetPodLogs().run({"pod": "gateway-1"}, make_ctx(k8s))

    assert result.ok
    assert result.data["line_count"] == 4
    assert result.data["first_time"] == "2026-10-20T00:01:00Z"
    assert result.data["last_time"] == "2026-10-20T00:03:00Z"
    assert "hunter2" not in result.model_dump_json()
    assert k8s.log_calls == [("gateway-1", "gateway", False, 100)]  # 컨테이너는 Pod 첫 번째
    saved = evidence.saved[0]
    assert saved.kind == "log_excerpt"
    assert saved.subject == "dev/Pod/gateway-1"
    assert result.summary == (
        "gateway-1/gateway 로그 4줄, 2026-10-20T00:01:00Z~2026-10-20T00:03:00Z"
    )


def test_pod_logs_grep_counts_matches(make_ctx):
    result = GetPodLogs().run({"pod": "gateway-1", "grep": "ERROR"}, make_ctx(_k8s()))

    assert result.data["match_count"] == 2
    assert result.data["lines"] == [
        "2026-10-20T00:02:00Z ERROR db password=*** refused",
        "2026-10-20T00:03:00Z ERROR retry",
    ]
    assert "'ERROR' 2건" in result.summary


def test_pod_logs_previous_container_with_explicit_container(make_ctx):
    k8s = _k8s({("gateway-1", "sidecar", True): "boom"})

    result = GetPodLogs().run(
        {"pod": "gateway-1", "container": "sidecar", "previous": True, "tail": 200},
        make_ctx(k8s),
    )

    assert result.data["previous"] is True
    assert k8s.log_calls == [("gateway-1", "sidecar", True, 200)]
    assert "이전 컨테이너" in result.summary


def test_pod_logs_long_lines_are_clipped(make_ctx):
    k8s = _k8s({("gateway-1", "gateway", False): "x" * 2000})

    result = GetPodLogs().run({"pod": "gateway-1"}, make_ctx(k8s))

    assert len(result.data["lines"][0]) < 600
    assert result.truncated


def test_pod_logs_failure_is_saved(make_ctx, evidence):
    k8s = _k8s({("gateway-1", "gateway", False): K8sCallError("404 Not Found")})

    result = GetPodLogs().run({"pod": "gateway-1"}, make_ctx(k8s))

    assert not result.ok
    assert "404 Not Found" in result.summary
    assert evidence.saved[0].kind == "log_excerpt"


def test_pod_logs_unknown_pod_fails(make_ctx):
    result = GetPodLogs().run({"pod": "nope"}, make_ctx(_k8s()))

    assert not result.ok
    assert "nope" in result.summary


@pytest.mark.parametrize("args", [{"pod": "a", "tail": 201}, {"pod": "a", "tail": 0}])
def test_pod_logs_tail_limit(make_ctx, args):
    with pytest.raises(ServiceException):
        GetPodLogs().run(args, make_ctx(_k8s()))


# ── get_events ──


def _events():
    return FakeK8s(
        {
            "Event": [
                event("BackOff", "Pod", "gateway-1", "2026-10-20T00:55:00Z", count=4),
                event("Failed", "Pod", "gateway-1", "2026-10-20T00:50:00Z", message="token=abc"),
                event("Scheduled", "Pod", "agent-1", "2026-10-20T00:59:00Z", type_="Normal"),
                event("BackOff", "Pod", "agent-1", "2026-10-19T23:00:00Z"),  # 2시간 전
            ]
        }
    )


def test_events_default_window_is_30_minutes_newest_first(make_ctx, evidence):
    result = GetEvents().run({}, make_ctx(_events()))

    assert result.ok
    assert [e["reason"] for e in result.data["events"]] == ["Scheduled", "BackOff", "Failed"]
    assert result.data["since"] == "2026-10-20T00:30:00+00:00"
    assert result.data["events"][1] == {
        "type": "Warning",
        "reason": "BackOff",
        "object": "Pod/gateway-1",
        "message": "m",
        "count": 4,
        "last": "2026-10-20T00:55:00Z",
    }
    assert "abc" not in result.model_dump_json()
    assert evidence.saved[0].kind == "event"
    assert evidence.saved[0].subject == "dev/Event/*"
    assert result.summary == "이벤트 3건 (Warning 2) 최근 30분: BackOff 1, Failed 1, Scheduled 1"


def test_events_filter_by_subject(make_ctx, evidence):
    result = GetEvents().run({"kind": "Pod", "name": "agent-1"}, make_ctx(_events()))

    assert [e["reason"] for e in result.data["events"]] == ["Scheduled"]
    assert evidence.saved[0].subject == "dev/Pod/agent-1"


def test_events_window_up_to_120_minutes(make_ctx):
    result = GetEvents().run({"since_minutes": 120}, make_ctx(_events()))

    assert len(result.data["events"]) == 4


@pytest.mark.parametrize("minutes", [0, 121])
def test_events_window_limit(make_ctx, minutes):
    with pytest.raises(ServiceException):
        GetEvents().run({"since_minutes": minutes}, make_ctx(_events()))


def test_events_failure_is_saved(make_ctx):
    result = GetEvents().run({}, make_ctx(FakeK8s({"Event": K8sCallError("403 Forbidden")})))

    assert not result.ok
    assert "403 Forbidden" in result.summary


def test_events_are_limited(make_ctx):
    many = [event("BackOff", "Pod", f"p-{i}", "2026-10-20T00:55:00Z") for i in range(30)]

    result = GetEvents().run({}, make_ctx(FakeK8s({"Event": many})))

    assert len(result.data["events"]) == 20
    assert result.data["count"] == 30
    assert result.truncated
