"""조사 요청 — 에이전트 입력 (상세설계 §2.1)."""

from typing import Literal

from pydantic import BaseModel, Field

from service.model.signal import SignalBoard
from service.model.topology import Topology

Trigger = Literal[
    "schedule", "check", "investigate", "ui"
]  # 주기 / /kg check / /kg investigate / 화면 실행


class InvestigationRequest(BaseModel):
    trigger: Trigger
    gate_path: Literal["A", "B"]  # 게이트가 열린 경우만 조사한다
    signal_board: SignalBoard
    topology: Topology = Topology()
    user_symptom: str | None = Field(default=None, max_length=500)  # 마스킹 후
