"""신호판: 원시 신호를 서비스별로 묶는다 (기획서 §6.2, 에이전트 입력).

통과한 신호는 뺀다 (적발·관찰 중·확인하지 못함만 남김). 서비스가 없는 신호(노드)는 대상으로 묶는다.
"""

from service.model import Signal, SignalBoard


def build_signal_board(signals: list[Signal]) -> SignalBoard:
    by_service: dict[str, list[Signal]] = {}
    for s in signals:
        if s.verdict != "passed":
            by_service.setdefault(s.service or s.subject, []).append(s)
    return SignalBoard(by_service=by_service)
