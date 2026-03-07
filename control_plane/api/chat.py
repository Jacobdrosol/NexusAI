import asyncio
import json
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from control_plane.security.guards import enforce_body_size, enforce_rate_limit
from shared.exceptions import BotNotFoundError, ConversationNotFoundError
from shared.models import ChatConversation, ChatMessage, Task, TaskMetadata

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
    context_item_ids: Optional[List[str]] = None


def _messages_to_payload(messages: List[ChatMessage], context_items: Optional[List[str]] = None) -> List[dict]:
    payload = [{"role": m.role, "content": m.content} for m in messages]
    if context_items:
        joined = "\n".join(context_items)
        payload.insert(0, {"role": "system", "content": f"Context:\n{joined}"})
    return payload


async def _resolve_context_items(request: Request, body: PostMessageRequest) -> List[str]:
    # Backward compatible direct context usage.
    resolved: List[str] = list(body.context_items or [])
    item_ids = [str(i).strip() for i in (body.context_item_ids or []) if str(i).strip()]
    if not item_ids:
        return resolved

    vault_manager = getattr(request.app.state, "vault_manager", None)
    if vault_manager is None:
        return resolved

    for item_id in item_ids[:20]:
        try:
            item = await vault_manager.get_item(item_id)
            text = (item.content or "").strip()
            if not text:
                continue
            # Bound payload size to reduce latency and accidental leakage.
            snippet = text[:4000]
            resolved.append(f"[vault:{item.id}] {item.title}\n{snippet}")
        except Exception:
            continue
    return resolved


def _extract_assign_instruction(content: str) -> Optional[str]:
    text = content.strip()
    if not text.lower().startswith("@assign"):
        return None
    instruction = text[len("@assign"):].strip()
    return instruction or None


def _extract_task_output(result: Any) -> str:
    if isinstance(result, dict):
        output = result.get("output")
        if output is not None:
            return str(output)
        return json.dumps(result)
    if result is None:
        return ""
    return str(result)


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


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, request: Request) -> None:
    chat_manager = request.app.state.chat_manager
    try:
        await chat_manager.delete_conversation(conversation_id)
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
    await enforce_body_size(request, route_name="chat_messages", default_max_bytes=200_000)
    await enforce_rate_limit(
        request,
        route_name="chat_messages",
        default_limit=120,
        default_window_seconds=60,
    )
    chat_manager = request.app.state.chat_manager
    scheduler = request.app.state.scheduler
    pm_orchestrator = request.app.state.pm_orchestrator
    try:
        conversation = await chat_manager.get_conversation(conversation_id)
        user_message = await chat_manager.add_message(
            conversation_id=conversation_id,
            role="user",
            content=body.content,
        )
        assign_instruction = _extract_assign_instruction(body.content)
        if assign_instruction is not None:
            resolved_context = await _resolve_context_items(request, body)
            assignment = await pm_orchestrator.orchestrate_assignment(
                conversation_id=conversation_id,
                instruction=assign_instruction,
                requested_pm_bot_id=body.bot_id,
                context_items=resolved_context,
                project_id=conversation.project_id,
            )
            completion = await pm_orchestrator.wait_for_completion(assignment)
            assistant_message = await pm_orchestrator.persist_summary_message(
                conversation_id=conversation_id,
                assignment=assignment,
                completion=completion,
            )
            return {
                "mode": "assign",
                "user_message": user_message,
                "assistant_message": assistant_message,
                "assignment": assignment,
                "completion": completion,
            }

        messages = await chat_manager.list_messages(conversation_id)
        target_bot_id = body.bot_id or conversation.default_bot_id
        if not target_bot_id:
            return {"user_message": user_message, "assistant_message": None}

        resolved_context = await _resolve_context_items(request, body)
        payload = _messages_to_payload(messages, context_items=resolved_context)
        task = Task(
            id=f"chat-{user_message.id}",
            bot_id=target_bot_id,
            payload=payload,
            metadata=TaskMetadata(
                source="chat",
                project_id=conversation.project_id,
                conversation_id=conversation_id,
            ),
            status="running",
            created_at=user_message.created_at,
            updated_at=user_message.created_at,
        )
        result = await scheduler.schedule(task)
        assistant_output = _extract_task_output(result)
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
    await enforce_body_size(request, route_name="chat_stream", default_max_bytes=200_000)
    await enforce_rate_limit(
        request,
        route_name="chat_stream",
        default_limit=60,
        default_window_seconds=60,
    )
    chat_manager = request.app.state.chat_manager
    scheduler = request.app.state.scheduler
    task_manager = request.app.state.task_manager
    pm_orchestrator = request.app.state.pm_orchestrator

    async def event_gen() -> AsyncGenerator[str, None]:
        try:
            conversation = await chat_manager.get_conversation(conversation_id)
            user_message = await chat_manager.add_message(
                conversation_id=conversation_id,
                role="user",
                content=body.content,
            )
            yield f"event: user_message\ndata: {user_message.model_dump_json()}\n\n"
            assign_instruction = _extract_assign_instruction(body.content)
            if assign_instruction is not None:
                resolved_context = await _resolve_context_items(request, body)
                assignment = await pm_orchestrator.orchestrate_assignment(
                    conversation_id=conversation_id,
                    instruction=assign_instruction,
                    requested_pm_bot_id=body.bot_id,
                    context_items=resolved_context,
                    project_id=conversation.project_id,
                )
                graph_payload = {
                    "orchestration_id": assignment.get("orchestration_id"),
                    "tasks": assignment.get("tasks", []),
                    "plan": assignment.get("plan", {}),
                }
                yield f"event: task_graph\ndata: {json.dumps(graph_payload)}\n\n"

                tracked_ids = [
                    str(t.get("id"))
                    for t in assignment.get("tasks", [])
                    if isinstance(t, dict) and t.get("id")
                ]
                last_status: Dict[str, str] = {}

                while True:
                    all_terminal = True
                    for task_id in tracked_ids:
                        task = await task_manager.get_task(task_id)
                        previous = last_status.get(task_id)
                        if previous != task.status:
                            title = ""
                            if isinstance(task.payload, dict):
                                title = str(task.payload.get("title") or "")
                            payload = {
                                "task_id": task.id,
                                "status": task.status,
                                "bot_id": task.bot_id,
                                "title": title,
                                "result": task.result if task.status == "completed" else None,
                                "error": (
                                    task.error.model_dump()
                                    if task.status == "failed" and task.error
                                    else None
                                ),
                            }
                            yield f"event: task_status\ndata: {json.dumps(payload)}\n\n"
                            last_status[task_id] = task.status
                        if task.status not in {"completed", "failed"}:
                            all_terminal = False
                    if all_terminal:
                        break
                    await asyncio.sleep(0.4)

                completion = await pm_orchestrator.wait_for_completion(assignment, max_wait_seconds=1.0)
                assistant_message = await pm_orchestrator.persist_summary_message(
                    conversation_id=conversation_id,
                    assignment=assignment,
                    completion=completion,
                )
                yield f"event: assistant_message\ndata: {assistant_message.model_dump_json()}\n\n"
                yield "event: done\ndata: {}\n\n"
                return

            messages = await chat_manager.list_messages(conversation_id)
            target_bot_id = body.bot_id or conversation.default_bot_id
            if not target_bot_id:
                yield "event: done\ndata: {}\n\n"
                return

            resolved_context = await _resolve_context_items(request, body)
            payload = _messages_to_payload(messages, context_items=resolved_context)
            task = Task(
                id=f"chat-{user_message.id}",
                bot_id=target_bot_id,
                payload=payload,
                metadata=TaskMetadata(
                    source="chat",
                    project_id=conversation.project_id,
                    conversation_id=conversation_id,
                ),
                status="running",
                created_at=user_message.created_at,
                updated_at=user_message.created_at,
            )
            result = await scheduler.schedule(task)
            assistant_output = _extract_task_output(result)
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
