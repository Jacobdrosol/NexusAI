import json
from typing import Any, AsyncGenerator, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from shared.exceptions import BotNotFoundError, ConversationNotFoundError
from shared.models import ChatConversation, ChatMessage, Task

router = APIRouter(prefix="/v1/chat", tags=["chat"])


class CreateConversationRequest(BaseModel):
    title: str
    project_id: Optional[str] = None
    scope: str = "global"
    default_bot_id: Optional[str] = None
    default_model_id: Optional[str] = None


class PostMessageRequest(BaseModel):
    content: str
    bot_id: Optional[str] = None
    context_items: Optional[List[str]] = None


def _messages_to_payload(messages: List[ChatMessage], context_items: Optional[List[str]] = None) -> List[dict]:
    payload = [{"role": m.role, "content": m.content} for m in messages]
    if context_items:
        joined = "\n".join(context_items)
        payload.insert(0, {"role": "system", "content": f"Context:\n{joined}"})
    return payload


@router.post("/conversations", response_model=ChatConversation)
async def create_conversation(request: Request, body: CreateConversationRequest) -> ChatConversation:
    chat_manager = request.app.state.chat_manager
    return await chat_manager.create_conversation(
        title=body.title,
        project_id=body.project_id,
        scope=body.scope,
        default_bot_id=body.default_bot_id,
        default_model_id=body.default_model_id,
    )


@router.get("/conversations", response_model=List[ChatConversation])
async def list_conversations(
    request: Request,
    project_id: Optional[str] = Query(default=None),
) -> List[ChatConversation]:
    chat_manager = request.app.state.chat_manager
    return await chat_manager.list_conversations(project_id=project_id)


@router.get("/conversations/{conversation_id}", response_model=ChatConversation)
async def get_conversation(conversation_id: str, request: Request) -> ChatConversation:
    chat_manager = request.app.state.chat_manager
    try:
        return await chat_manager.get_conversation(conversation_id)
    except ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/conversations/{conversation_id}/messages", response_model=List[ChatMessage])
async def list_messages(conversation_id: str, request: Request) -> List[ChatMessage]:
    chat_manager = request.app.state.chat_manager
    try:
        return await chat_manager.list_messages(conversation_id)
    except ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/conversations/{conversation_id}/messages")
async def post_message(conversation_id: str, request: Request, body: PostMessageRequest) -> dict:
    chat_manager = request.app.state.chat_manager
    scheduler = request.app.state.scheduler
    try:
        conversation = await chat_manager.get_conversation(conversation_id)
        user_message = await chat_manager.add_message(
            conversation_id=conversation_id,
            role="user",
            content=body.content,
        )
        messages = await chat_manager.list_messages(conversation_id)
        target_bot_id = body.bot_id or conversation.default_bot_id
        if not target_bot_id:
            return {"user_message": user_message, "assistant_message": None}

        payload = _messages_to_payload(messages, context_items=body.context_items)
        task = Task(
            id=f"chat-{user_message.id}",
            bot_id=target_bot_id,
            payload=payload,
            status="running",
            created_at=user_message.created_at,
            updated_at=user_message.created_at,
        )
        result = await scheduler.schedule(task)
        assistant_output = result.get("output", "") if isinstance(result, dict) else str(result)
        assistant_message = await chat_manager.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=assistant_output,
            bot_id=target_bot_id,
        )
        return {"user_message": user_message, "assistant_message": assistant_message}
    except ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/conversations/{conversation_id}/stream")
async def stream_message(conversation_id: str, request: Request, body: PostMessageRequest) -> StreamingResponse:
    chat_manager = request.app.state.chat_manager
    scheduler = request.app.state.scheduler

    async def event_gen() -> AsyncGenerator[str, None]:
        try:
            conversation = await chat_manager.get_conversation(conversation_id)
            user_message = await chat_manager.add_message(
                conversation_id=conversation_id,
                role="user",
                content=body.content,
            )
            yield f"event: user_message\ndata: {user_message.model_dump_json()}\n\n"

            messages = await chat_manager.list_messages(conversation_id)
            target_bot_id = body.bot_id or conversation.default_bot_id
            if not target_bot_id:
                yield "event: done\ndata: {}\n\n"
                return

            payload = _messages_to_payload(messages, context_items=body.context_items)
            task = Task(
                id=f"chat-{user_message.id}",
                bot_id=target_bot_id,
                payload=payload,
                status="running",
                created_at=user_message.created_at,
                updated_at=user_message.created_at,
            )
            result = await scheduler.schedule(task)
            assistant_output = result.get("output", "") if isinstance(result, dict) else str(result)
            assistant_message = await chat_manager.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=assistant_output,
                bot_id=target_bot_id,
            )
            yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
            yield "event: done\ndata: {}\n\n"
        except ConversationNotFoundError:
            payload = json.dumps({"error": "conversation_not_found"})
            yield f"event: error\ndata: {payload}\n\n"
        except BotNotFoundError:
            payload = json.dumps({"error": "bot_not_found"})
            yield f"event: error\ndata: {payload}\n\n"
        except Exception as e:
            payload = json.dumps({"error": str(e)})
            yield f"event: error\ndata: {payload}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
