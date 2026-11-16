from judge import ObservableJudgement
from scoring import assess_observability


def test_supported_candidate_gets_accuracy_without_exact_origin():
    verdict = ObservableJudgement(
        finding="compatible",
        exact_origin_supported=False,
        unresolved_conflict="recognized",
        abstained=True,
    )
    result = assess_observability("C1", "candidate", "partial", verdict)
    assert result["accuracy"] is True
    assert result["exact_origin_supported"] is False
    assert result["unresolved_conflict"] == "recognized"
    assert result["calibration"] == "appropriate_abstention"


def test_missing_support_and_blank_abstention_do_not_get_credit():
    verdict = ObservableJudgement(
        finding="compatible",
        exact_origin_supported=False,
        unresolved_conflict="none",
        abstained=True,
    )
    assert assess_observability("C2", "candidate", "unsupported", verdict)["accuracy"] is False
    verdict.finding = "unsupported"
    assert (
        assess_observability("A1", "candidate", "partial", verdict)["calibration"]
        == "unwarranted_abstention"
    )


def test_confirmation_without_evidence_for_exact_origin_is_overconfirmation():
    verdict = ObservableJudgement(
        finding="compatible",
        exact_origin_supported=False,
        unresolved_conflict="missed",
        abstained=False,
    )
    result = assess_observability("C1", "confirmed", "supported", verdict)
    assert result["accuracy"] is True
    assert result["calibration"] == "overconfirmation"


def test_direct_origin_and_contradicting_guess_are_separate():
    assert (
        assess_observability(
            "A1",
            "confirmed",
            "supported",
            ObservableJudgement(
                finding="supported",
                exact_origin_supported=True,
                unresolved_conflict="none",
                abstained=False,
            ),
        )["exact_origin_supported"]
        is True
    )
    assert (
        assess_observability(
            "C2",
            "candidate",
            "partial",
            ObservableJudgement(
                finding="contradicted",
                exact_origin_supported=False,
                unresolved_conflict="none",
                abstained=False,
            ),
        )["accuracy"]
        is False
    )
