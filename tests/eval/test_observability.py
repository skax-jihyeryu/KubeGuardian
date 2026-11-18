import pytest

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


@pytest.mark.parametrize(
    ("scenario", "judgement", "support", "verdict", "calibration"),
    [
        # A1: 설정에서 원인이 보이는 사례 — 근거가 있는데 보류하면 부적절한 보류
        (
            "A1",
            "candidate",
            "supported",
            ("supported", True, "none", True),
            "unwarranted_abstention",
        ),
        ("A1", "confirmed", "supported", ("supported", True, "none", False), "appropriate"),
        # C2: 관측 제한 — admin 정체까지 지지하고 Redis 차단을 보류하면 적절한 보류
        (
            "C2",
            "candidate",
            "partial",
            ("compatible", False, "none", True),
            "appropriate_abstention",
        ),
        # C2: Redis 차단을 직접 관측 없이 확정하면 과도한 확정
        ("C2", "confirmed", "partial", ("compatible", False, "none", False), "overconfirmation"),
        # C1: 충돌 관측 — 충돌을 인식하고 보류하면 적절한 보류
        (
            "C1",
            "candidate",
            "partial",
            ("compatible", False, "recognized", True),
            "appropriate_abstention",
        ),
        # C1: 출처가 직접 지지돼도 미해결 충돌이 남은 채 확정하면 과도한 확정
        (
            "C1",
            "confirmed",
            "supported",
            ("supported", True, "recognized", False),
            "overconfirmation",
        ),
        # 증상만 반복한 빈 보류는 어느 시나리오에서도 적절한 보류가 아니다
        (
            "C1",
            "candidate",
            "unsupported",
            ("unsupported", False, "missed", True),
            "unwarranted_abstention",
        ),
    ],
)
def test_contract_distinguishes_visible_limited_and_conflict_cases(
    scenario, judgement, support, verdict, calibration
):
    finding, exact, conflict, abstained = verdict
    result = assess_observability(
        scenario,
        judgement,
        support,
        ObservableJudgement(
            finding=finding,
            exact_origin_supported=exact,
            unresolved_conflict=conflict,
            abstained=abstained,
        ),
    )
    assert result["calibration"] == calibration
