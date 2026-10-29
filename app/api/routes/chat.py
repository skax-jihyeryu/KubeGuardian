"""이력 질의 채팅. 저장된 점검·이슈·알림 이력으로만 답한다."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import Engine

from api.deps import get_config, get_engine, get_llm_factory
from api.schemas.chat import ChatMessageIn
from core.config import KubeGuardianConfig
from service.agent.base import LlmFactory
from service.chat import ChatReply, answer_question

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/messages")
def post_message(
    body: ChatMessageIn,
    engine: Annotated[Engine, Depends(get_engine)],
    config: Annotated[KubeGuardianConfig, Depends(get_config)],
    llm_factory: Annotated[LlmFactory, Depends(get_llm_factory)],
) -> ChatReply:
    return answer_question(
        body.question,
        engine=engine,
        config=config,
        llm_factory=llm_factory,
        conversation_id=body.conversation_id,
    )
