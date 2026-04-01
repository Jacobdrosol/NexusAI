from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from control_plane.platform_ai.session_store import PlatformAISessionStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlatformAISessionRuntime:
    """In-process runtime loop for Platform AI sessions.

    The runtime is intentionally deterministic and transparent:
    - operator messages are persisted and acknowledged
    - loop heartbeats emit action_trace events
    - deploy actions are executed via dashboard DeployManager and streamed back as traces
    """

    def __init__(self, store: PlatformAISessionStore) -> None:
        self._store = store
        self._session_tasks: Dict[str, asyncio.Task[None]] = {}
        self._deploy_tasks: Dict[str, asyncio.Task[None]] = {}
        self._processed_operator_messages: Dict[str, set[str]] = {}

    async def ensure_session_loop(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        task = self._session_tasks.get(sid)
        if task is not None and not task.done():
            return
        self._session_tasks[sid] = asyncio.create_task(self._session_loop(sid))

    async def stop_session_loop(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        task = self._session_tasks.get(sid)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def post_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        message = await self._store.append_message(
            session_id,
            role=role,
            content=content,
            metadata=metadata,
        )
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "session_message",
                "role": role,
                "message_id": message.get("id"),
                "content_preview": str(content or "")[:280],
            },
        )
        session = await self._store.get_session(session_id)
        status = str((session or {}).get("status") or "").strip().lower()
        if session is not None and status == "active":
            await self.ensure_session_loop(session_id)
        elif str(role or "").strip().lower() == "operator":
            await self._store.append_message(
                session_id,
                role="assistant",
                content=(
                    "Message received. Session is not active right now. "
                    "Resume/start the session to execute actions from this instruction."
                ),
                metadata={"source": "session_state_ack", "session_status": status or "unknown"},
            )
        return message

    async def start_deploy_run(self, session_id: str, *, requested_by: str) -> Dict[str, Any]:
        sid = str(session_id or "").strip()
        if not sid:
            return {"status": "error", "detail": "session_id is required"}
        existing = self._deploy_tasks.get(sid)
        if existing is not None and not existing.done():
            return {"status": "running", "detail": "deploy runner already active"}
        self._deploy_tasks[sid] = asyncio.create_task(self._deploy_loop(sid, requested_by=requested_by))
        return {"status": "started"}

    async def _session_loop(self, session_id: str) -> None:
        phases = ["observe", "diagnose", "tune", "verify"]
        action_cycle = [
            {"action": "analyze_context", "kind": "decision", "tool": None, "detail": "Inspect assignment and pipeline state."},
            {"action": "select_tool", "kind": "tool", "tool": "task_graph_inspector", "detail": "Scan graph and branch/join outcomes."},
            {"action": "evaluate_quality", "kind": "decision", "tool": None, "detail": "Evaluate output quality signals and gaps."},
            {"action": "propose_change", "kind": "outcome", "tool": None, "detail": "Prepare tuning or control recommendations."},
        ]
        phase_index = 0
        tick = 0
        await self._store.append_event(
            session_id,
            "action_trace",
            {"action": "runtime_loop_started", "started_at": _now()},
        )
        try:
            while True:
                session = await self._store.get_session(session_id)
                if session is None:
                    break
                status = str(session.get("status") or "").strip().lower()
                if status in {"stopped", "completed", "failed", "archived"}:
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {"action": "runtime_loop_stopped", "status": status, "stopped_at": _now()},
                    )
                    break
                if status == "paused":
                    await asyncio.sleep(1.0)
                    continue

                tick += 1
                phase = phases[phase_index % len(phases)]
                step = action_cycle[(tick - 1) % len(action_cycle)]
                phase_index += 1

                await self._process_operator_messages(session_id)
                last_tool = str(step.get("tool") or "").strip() or None
                active_action = str(step.get("action") or "").strip() or "runtime_tick"
                await self._store.update_session(
                    session_id,
                    metadata={
                        "runtime_tick": tick,
                        "current_phase": phase,
                        "active_action": active_action,
                        "last_tool_call": last_tool,
                        "last_heartbeat_at": _now(),
                    },
                )
                await self._store.append_event(
                    session_id,
                    "action_trace",
                    {
                        "action": "runtime_tick",
                        "phase": phase,
                        "tick": tick,
                        "kind": str(step.get("kind") or "decision"),
                        "active_action": active_action,
                        "tool": last_tool,
                        "detail": str(step.get("detail") or ""),
                    },
                )
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            await self._store.append_event(
                session_id,
                "action_trace",
                {"action": "runtime_loop_cancelled", "cancelled_at": _now()},
            )
            raise
        finally:
            current = self._session_tasks.get(session_id)
            if current is not None and current.done():
                self._session_tasks.pop(session_id, None)

    async def _process_operator_messages(self, session_id: str) -> None:
        seen = self._processed_operator_messages.setdefault(session_id, set())
        messages = await self._store.list_messages(session_id, limit=300)
        for message in messages:
            if str(message.get("role") or "").strip().lower() != "operator":
                continue
            mid = str(message.get("id") or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            content = str(message.get("content") or "").strip()
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "operator_message_received",
                    "message_id": mid,
                    "content_preview": content[:280],
                    "kind": "decision",
                },
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=f"Acknowledged. Applying operator direction: {content[:500]}",
                metadata={"source": "runtime_ack", "operator_message_id": mid},
            )
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "operator_message_acknowledged",
                    "message_id": mid,
                    "kind": "outcome",
                    "detail": "Operator instruction has been accepted into session workflow.",
                },
            )

    async def _deploy_loop(self, session_id: str, *, requested_by: str) -> None:
        last_log_len = 0
        await self._store.append_event(
            session_id,
            "action_trace",
            {"action": "deploy_runner_started", "requested_by": requested_by, "started_at": _now()},
        )
        try:
            try:
                from dashboard.deploy_manager import DeployManager
            except Exception as exc:
                await self._store.append_event(
                    session_id,
                    "action_trace",
                    {"action": "deploy_runner_error", "detail": f"deploy manager unavailable: {exc}"},
                )
                return

            manager = DeployManager.instance()
            ok, message = manager.start(requested_by=requested_by or "platform-ai")
            await self._store.append_event(
                session_id,
                "action_trace",
                {"action": "deploy_requested", "ok": bool(ok), "message": str(message or "")},
            )
            if not ok:
                return
            while True:
                status = manager.status(refresh_remote=False)
                logs = status.get("log_tail") if isinstance(status.get("log_tail"), list) else []
                for line in logs[last_log_len:]:
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {"action": "deploy_log", "line": str(line)},
                    )
                last_log_len = len(logs)
                state = str(status.get("state") or "").strip().lower()
                if state in {"succeeded", "failed"}:
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {
                            "action": "deploy_finished",
                            "state": state,
                            "last_error": status.get("last_error"),
                            "finished_at": status.get("finished_at"),
                        },
                    )
                    if state == "failed":
                        await self._store.append_message(
                            session_id,
                            role="assistant",
                            content=(
                                "Deployment failed. Captured logs were added to action trace. "
                                "Apply fixes, commit/push, then trigger deploy again."
                            ),
                            metadata={"source": "deploy_runner", "state": "failed"},
                        )
                    break
                await asyncio.sleep(2.0)
        finally:
            self._deploy_tasks.pop(session_id, None)
