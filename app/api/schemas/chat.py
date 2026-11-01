"""이력 질의 API 요청 모델. 응답은 service.chat.ChatReply를 그대로 쓴다."""

from pydantic import BaseModel, Field


class ChatMessageIn(BaseModel):
    question: str
    conversation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,32}$")
