from core.guard.validate import validate_answer
from service.model.chat import AnswerSentence, ChatAnswer


def _validate(sentences):
    return validate_answer(
        ChatAnswer(sentences=sentences),
        evidence=lambda i: i == "ev-0001",
        issue=lambda i: i == 12,
        run=lambda i: i == 340,
    )


def test_unknown_ids_removed_and_estimated():
    s = AnswerSentence(text="A", kind="fact", evidence_ids=["ev-0001", "ev-9999", "ev-12x"])
    out = _validate([s]).sentences[0]
    assert out.evidence_ids == ["ev-0001"]
    assert out.text == "A"


def test_no_citation_fact_estimated():
    s = AnswerSentence(text="A", kind="fact", evidence_ids=["ev-9999"], issue_ids=[0])
    once = _validate([s])
    assert once.sentences[0].text.endswith(" (추정)")
    twice = _validate(once.sentences)
    assert twice.sentences[0].text.count("(추정)") == 1


def test_issue_citation_counts():
    s = AnswerSentence(text="A", kind="fact", issue_ids=[12])
    assert _validate([s]).sentences[0].text == "A"


def test_run_citation_counts_and_unknown_removed():
    s = AnswerSentence(text="A", kind="interpretation", run_ids=[340, 5])
    out = _validate([s]).sentences[0]
    assert out.run_ids == [340]
    assert out.text == "A"


def test_guide_not_estimated():
    s = AnswerSentence(text="A", kind="guide")
    assert _validate([s]).sentences[0].text == "A"
