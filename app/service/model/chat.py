"""이력 질의 채팅 모델. ChatAnswer는 LLM 출력 스키마이자 최종 답이다."""

from typing import Literal

from pydantic import BaseModel, Field

SentenceKind = Literal["fact", "guide", "interpretation"]  # 사실 / 안내 / 해석


class AnswerSentence(BaseModel):
    text: str
    kind: SentenceKind
    evidence_ids: list[str] = []
    issue_ids: list[int] = []
    run_ids: list[int] = []


class ChatAnswer(BaseModel):
    sentences: list[AnswerSentence]
    current_state: bool = False  # 지금 상태를 묻는 질문
    wants_investigation: bool = False  # 증상 조사를 원하는 질문
    partial: bool = False  # 호출 상한 도달. 코드만 세팅한다 (프롬프트에서 요구하지 않음)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class ChatRequest(BaseModel):
    question: str = Field(max_length=500)
    history: list[ChatTurn] = []
