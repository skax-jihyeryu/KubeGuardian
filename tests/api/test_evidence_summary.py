from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.api.seed import add_evidence, at, finish_check, make_run


class SummaryModel:
    def __init__(self, text="응답이 실패했습니다.", error=None):
        self.text, self.error, self.messages = text, error, []

    def invoke(self, messages):
        self.messages.append(messages)
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.text)


def setup_evidence(engine, **kwargs):
    run = make_run(engine)
    finish_check(engine, run, path="C")
    ev = add_evidence(engine, run, **kwargs)
    return run, ev


def test_summary_masks_input_output_and_reuses_cache(engine):
    from service.evidence_summary import summarize_evidence

    _, ev = setup_evidence(engine, data={"password": "input-secret", "status": 502})
    model = SummaryModel("HTTP 502. password=output-secret")
    names = []

    def factory(name):
        names.append(name)
        return model

    first = summarize_evidence(engine, ev, model="custom-model", llm_factory=factory, now=at(60))
    second = summarize_evidence(engine, ev, model="changed-model", llm_factory=factory, now=at(61))
    assert first.status == second.status == "done"
    assert second.text == first.text
    assert names == ["custom-model"]
    assert "input-secret" not in str(model.messages)
    assert "output-secret" not in first.text
    assert "***" in first.text


def test_failure_requires_explicit_retry(engine):
    from service.evidence_summary import summarize_evidence

    _, ev = setup_evidence(engine)
    bad = SummaryModel(error=RuntimeError("password=internal-secret"))
    args = dict(model="test", llm_factory=lambda _: bad, now=at(60))
    assert summarize_evidence(engine, ev, **args).status == "failed"
    good = SummaryModel()
    args.update(llm_factory=lambda _: good)
    assert summarize_evidence(engine, ev, **args).status == "failed"
    assert not good.messages
    assert summarize_evidence(engine, ev, retry=True, **args).status == "done"


@pytest.mark.parametrize("text", ["", "   ", [{"type": "unknown", "value": "bad"}]])
def test_empty_or_nontext_summary_is_failure(engine, text):
    from service.evidence_summary import summarize_evidence

    _, ev = setup_evidence(engine)
    result = summarize_evidence(
        engine, ev, model="test", llm_factory=lambda _: SummaryModel(text), now=at(60)
    )
    assert result.status == "failed"


def test_input_size_limit_is_disclosed(engine):
    from service.evidence_summary import INPUT_LIMIT, summarize_evidence

    _, ev = setup_evidence(engine, data={"lines": ["A" * 30000]})
    model = SummaryModel()
    result = summarize_evidence(engine, ev, model="test", llm_factory=lambda _: model, now=at(60))
    assert result.truncated
    assert len(model.messages[0][-1].content) <= INPUT_LIMIT + 100


def test_claim_is_atomic_and_stale_completion_cannot_overwrite_retry(engine):
    from core.db.evidence_summaries import PENDING_TTL, EvidenceSummaryStore

    _, ev = setup_evidence(engine)
    store = EvidenceSummaryStore(engine)
    with ThreadPoolExecutor(max_workers=4) as pool:
        tokens = list(pool.map(lambda _: store.claim(ev, "test", at(60)), range(4)))
    winners = [t for t in tokens if t]
    assert len(winners) == 1
    assert store.claim(ev, "test", at(61), retry=True) is None
    later = at(60) + PENDING_TTL + timedelta(seconds=1)
    assert store.get(ev, later).status == "failed"
    assert store.claim(ev, "test", later) is None
    new = store.claim(ev, "test", later, retry=True)
    assert new and new != winners[0]
    store.finish(ev, new, "새 요약", False, later)
    store.finish(ev, winners[0], "옛 요약", False, later)
    assert store.get(ev, later).text == "새 요약"


def test_drawer_load_and_summary_routes_are_scoped(client, engine):
    from core.db.evidence_summaries import EvidenceSummaryStore
    from deps import get_evidence_summary_factory

    run, ev = setup_evidence(engine, data={"status": 502})
    n = int(ev[3:])
    model = SummaryModel("<script>alert(1)</script> HTTP 502")
    client.app.dependency_overrides[get_evidence_summary_factory] = lambda: lambda _: model
    url = f"/dev/checks/{run}/evidence/{n}/summary"
    drawer = client.get(f"/dev/checks/{run}?ev={n}").text
    assert f'hx-post="{url}"' in drawer
    assert 'hx-trigger="load"' in drawer
    assert "<summary>원본 JSON</summary>" in drawer
    assert not model.messages
    assert EvidenceSummaryStore(engine).get(ev, at(60)) is None
    res = client.post(url)
    assert res.status_code == 200
    assert "AI 요약" in res.text
    assert "&lt;script&gt;" in res.text and "<script>" not in res.text
    assert "HTTP 502" in client.get(url).text
    assert "hx-post=" not in client.get(f"/dev/checks/{run}?ev={n}").text
    client.post(url)
    assert len(model.messages) == 1
    other = make_run(engine)
    for wrong in [
        f"/dev/checks/{other}/evidence/{n}/summary",
        f"/dev/checks/{run}/evidence/99999/summary",
        f"/other/checks/{run}/evidence/{n}/summary",
    ]:
        assert client.post(wrong).status_code == 404
        assert client.get(wrong).status_code == 404
    assert len(model.messages) == 1


def test_failed_fragment_keeps_raw_and_retry(client, engine):
    from deps import get_evidence_summary_factory

    run, ev = setup_evidence(engine, data={"status": 502})
    model = SummaryModel(error=RuntimeError("internal-secret"))
    client.app.dependency_overrides[get_evidence_summary_factory] = lambda: lambda _: model
    url = f"/dev/checks/{run}/evidence/{int(ev[3:])}/summary"
    text = client.post(url).text
    assert "다시 시도" in text
    assert "internal-secret" not in text
    assert "<details open id=" in text
    assert 'hx-swap-oob="outerHTML"' in text
    assert "502" in text
    assert "every 2s" not in text
    client.post(url)
    assert len(model.messages) == 1
    model.error = None
    assert "응답이 실패했습니다." in client.post(url + "?retry=true").text
    assert len(model.messages) == 2


def test_pending_fragment_polls_without_generating_again(client, engine):
    from core.db.evidence_summaries import EvidenceSummaryStore

    run, ev = setup_evidence(engine)
    EvidenceSummaryStore(engine).claim(ev, "test", at(20))
    text = client.get(f"/dev/checks/{run}/evidence/{int(ev[3:])}/summary").text
    assert "요약 생성 중" in text
    assert 'hx-trigger="every 2s"' in text
    assert "hx-post=" not in text


def test_polling_does_not_replace_raw_json(client, engine):
    from core.db.evidence_summaries import EvidenceSummaryStore

    run, ev = setup_evidence(engine, data={"status": 502})
    EvidenceSummaryStore(engine).claim(ev, "test", at(20))
    url = f"/dev/checks/{run}/evidence/{int(ev[3:])}/summary"
    assert "<summary>원본 JSON</summary>" not in client.get(url).text
    assert "<summary>원본 JSON</summary>" in client.get(f"/dev/checks/{run}?ev={int(ev[3:])}").text
