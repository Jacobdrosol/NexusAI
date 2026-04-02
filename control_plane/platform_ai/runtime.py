from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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

    def __init__(
        self,
        store: PlatformAISessionStore,
        *,
        assignment_service: Any = None,
        run_store: Any = None,
        task_manager: Any = None,
        bot_registry: Any = None,
    ) -> None:
        self._store = store
        self._assignment_service = assignment_service
        self._run_store = run_store
        self._task_manager = task_manager
        self._bot_registry = bot_registry
        self._session_tasks: Dict[str, asyncio.Task[None]] = {}
        self._deploy_tasks: Dict[str, asyncio.Task[None]] = {}
        self._processed_operator_messages: Dict[str, set[str]] = {}
        self._last_progress_signature: Dict[str, str] = {}
        self._last_heartbeat_ts: Dict[str, float] = {}
        self._bot_name_cache: Dict[str, str] = {}

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
        await self._store.append_event(
            session_id,
            "action_trace",
            {"action": "runtime_loop_started", "started_at": _now()},
        )
        self._last_heartbeat_ts[session_id] = time.monotonic()
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

                await self._process_operator_messages(session_id)

                snapshot = await self._build_progress_snapshot(session)
                signature = str(snapshot.get("signature") or "")
                previous_signature = self._last_progress_signature.get(session_id)
                status_counts = snapshot.get("status_counts") if isinstance(snapshot.get("status_counts"), dict) else {}
                active_tasks = snapshot.get("active_tasks") if isinstance(snapshot.get("active_tasks"), list) else []
                phase = str(snapshot.get("phase") or "observe")
                active_action = str(snapshot.get("active_action") or "monitor_orchestration")
                last_tool = "task_graph_inspector" if snapshot.get("orchestration_id") else None

                await self._store.update_session(
                    session_id,
                    metadata={
                        "runtime_tick": int(snapshot.get("tick") or 0),
                        "current_phase": phase,
                        "active_action": active_action,
                        "last_tool_call": last_tool,
                        "runtime_state": snapshot.get("runtime_state"),
                        "last_heartbeat_at": _now(),
                    },
                )
                now_mono = time.monotonic()
                changed = bool(signature) and signature != previous_signature
                heartbeat_due = (now_mono - float(self._last_heartbeat_ts.get(session_id) or 0.0)) >= 30.0

                if changed:
                    self._last_progress_signature[session_id] = signature
                    self._last_heartbeat_ts[session_id] = now_mono
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {
                            "action": "runtime_progress",
                            "kind": "decision",
                            "phase": phase,
                            "tick": int(snapshot.get("tick") or 0),
                            "active_action": active_action,
                            "tool": last_tool,
                            "detail": str(snapshot.get("detail") or ""),
                            "runtime_state": snapshot.get("runtime_state"),
                        },
                    )
                elif heartbeat_due:
                    self._last_heartbeat_ts[session_id] = now_mono
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {
                            "action": "runtime_heartbeat",
                            "kind": "outcome",
                            "phase": phase,
                            "active_action": active_action,
                            "detail": str(snapshot.get("heartbeat_detail") or "Monitoring session target."),
                            "runtime_state": snapshot.get("runtime_state"),
                        },
                    )

                if not snapshot.get("orchestration_id"):
                    await asyncio.sleep(4.0)
                elif bool(status_counts.get("running")) or bool(active_tasks):
                    await asyncio.sleep(1.5)
                else:
                    await asyncio.sleep(3.0)
        except asyncio.CancelledError:
            await self._store.append_event(
                session_id,
                "action_trace",
                {"action": "runtime_loop_cancelled", "cancelled_at": _now()},
            )
            raise
        finally:
            current = self._session_tasks.get(session_id)
            if current is asyncio.current_task():
                self._session_tasks.pop(session_id, None)
            self._last_progress_signature.pop(session_id, None)
            self._last_heartbeat_ts.pop(session_id, None)

    async def _process_operator_messages(self, session_id: str) -> None:
        seen = self._processed_operator_messages.setdefault(session_id, set())
        messages = await self._store.list_messages(session_id, limit=300)
        acknowledged_ids: set[str] = set()
        for message in messages:
            role = str(message.get("role") or "").strip().lower()
            if role != "assistant":
                continue
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            source = str(metadata.get("source") or "").strip().lower()
            message_id = str(metadata.get("operator_message_id") or "").strip()
            if source == "runtime_ack" and message_id:
                acknowledged_ids.add(message_id)
        for message in messages:
            if str(message.get("role") or "").strip().lower() != "operator":
                continue
            mid = str(message.get("id") or "").strip()
            if not mid or mid in seen or mid in acknowledged_ids:
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

    async def _bot_label(self, bot_id: str) -> str:
        raw = str(bot_id or "").strip()
        if not raw:
            return ""
        cached = self._bot_name_cache.get(raw)
        if cached:
            return cached
        label = raw
        if self._bot_registry is not None:
            try:
                bot = await self._bot_registry.get(raw)
                name = str(getattr(bot, "name", "") or "").strip()
                if name:
                    label = f"{name} ({raw})"
            except Exception:
                label = raw
        self._bot_name_cache[raw] = label
        return label

    async def _resolve_context(self, session: Dict[str, Any]) -> Dict[str, Any]:
        resolved_assignment_id = str(session.get("assignment_id") or "").strip() or None
        resolved_run_id = str(session.get("run_id") or "").strip() or None
        resolved_orchestration_id = str(session.get("orchestration_id") or "").strip() or None
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}

        run: Optional[Dict[str, Any]] = None
        if self._run_store is not None:
            try:
                if resolved_run_id:
                    run = await self._run_store.get_run(resolved_run_id)
                elif resolved_orchestration_id:
                    run = await self._run_store.get_run_by_orchestration(resolved_orchestration_id)
                elif resolved_assignment_id:
                    run = await self._run_store.get_latest_run_for_assignment(resolved_assignment_id)
            except Exception:
                run = None
        if isinstance(run, dict):
            resolved_assignment_id = str(run.get("assignment_id") or "").strip() or resolved_assignment_id
            resolved_run_id = str(run.get("id") or "").strip() or resolved_run_id
            resolved_orchestration_id = str(run.get("orchestration_id") or "").strip() or resolved_orchestration_id

        graph: Dict[str, Any] = {"nodes": [], "edges": []}
        tasks: List[Dict[str, Any]] = []
        if self._assignment_service is not None and (resolved_run_id or resolved_orchestration_id):
            try:
                graph_resp = await self._assignment_service.get_graph(
                    run_id=resolved_run_id,
                    orchestration_id=resolved_orchestration_id,
                )
            except Exception:
                graph_resp = {}
            if isinstance(graph_resp.get("graph"), dict):
                graph = graph_resp["graph"]
            raw_tasks = graph_resp.get("tasks")
            if isinstance(raw_tasks, list):
                tasks = [task for task in raw_tasks if isinstance(task, dict)]
        if not tasks and self._task_manager is not None and resolved_orchestration_id:
            try:
                listed = await self._task_manager.list_tasks(orchestration_id=resolved_orchestration_id, limit=1000)
                tasks = [task.model_dump() for task in listed]
            except Exception:
                tasks = []
        if not graph.get("nodes") and isinstance(run, dict) and isinstance(run.get("graph_snapshot"), dict):
            graph = run.get("graph_snapshot")  # type: ignore[assignment]

        return {
            "assignment_id": resolved_assignment_id,
            "run_id": resolved_run_id,
            "orchestration_id": resolved_orchestration_id,
            "pipeline_bot_id": str(metadata.get("pipeline_bot_id") or "").strip() or None,
            "pipeline_name": str(metadata.get("pipeline_name") or "").strip() or None,
            "graph": graph if isinstance(graph, dict) else {"nodes": [], "edges": []},
            "tasks": tasks,
        }

    async def _build_progress_snapshot(self, session: Dict[str, Any]) -> Dict[str, Any]:
        context = await self._resolve_context(session)
        tasks = context.get("tasks") if isinstance(context.get("tasks"), list) else []
        graph = context.get("graph") if isinstance(context.get("graph"), dict) else {"nodes": [], "edges": []}
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        status_counts: Dict[str, int] = {}
        task_rows: List[Dict[str, Any]] = []
        for row in tasks:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").strip().lower() or "unknown"
            status_counts[status] = int(status_counts.get(status) or 0) + 1
            task_rows.append(row)

        task_rows.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("id") or "")), reverse=True)
        active_tasks: List[Dict[str, Any]] = []
        for row in task_rows:
            status = str(row.get("status") or "").strip().lower()
            if status not in {"running", "queued", "blocked"}:
                continue
            bot_id = str(row.get("bot_id") or "").strip()
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            active_tasks.append(
                {
                    "task_id": str(row.get("id") or "").strip(),
                    "status": status,
                    "bot_id": bot_id or None,
                    "bot": await self._bot_label(bot_id) if bot_id else None,
                    "step_id": str(meta.get("step_id") or "").strip() or None,
                    "updated_at": str(row.get("updated_at") or "").strip() or None,
                }
            )
            if len(active_tasks) >= 8:
                break

        latest = task_rows[0] if task_rows else None
        latest_status = str((latest or {}).get("status") or "").strip().lower() if isinstance(latest, dict) else ""
        latest_bot_id = str((latest or {}).get("bot_id") or "").strip() if isinstance(latest, dict) else ""
        latest_task: Optional[Dict[str, Any]] = None
        if isinstance(latest, dict):
            latest_task = {
                "task_id": str(latest.get("id") or "").strip() or None,
                "status": latest_status or None,
                "bot_id": latest_bot_id or None,
                "bot": await self._bot_label(latest_bot_id) if latest_bot_id else None,
                "updated_at": str(latest.get("updated_at") or "").strip() or None,
            }

        focus_nodes: List[Dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_status = str(node.get("status") or "").strip().lower() or "queued"
            if node_status in {"succeeded", "skipped"}:
                continue
            node_id = str(node.get("id") or node.get("bot_id") or "").strip()
            if not node_id:
                continue
            focus_nodes.append(
                {
                    "node_id": node_id,
                    "title": str(node.get("title") or "").strip() or node_id,
                    "status": node_status,
                    "stage_kind": str(node.get("stage_kind") or "").strip() or None,
                }
            )
            if len(focus_nodes) >= 8:
                break

        total_tasks = len(task_rows)
        completed_like = (
            int(status_counts.get("completed") or 0)
            + int(status_counts.get("failed") or 0)
            + int(status_counts.get("cancelled") or 0)
            + int(status_counts.get("retried") or 0)
        )
        progress_ratio = (completed_like / total_tasks) if total_tasks else 0.0
        phase = "observe"
        active_action = "monitor_pipeline"
        if not context.get("orchestration_id"):
            active_action = "await_target_attachment"
            phase = "observe"
        elif int(status_counts.get("running") or 0) > 0:
            active_action = "monitor_running_bots"
            phase = "diagnose"
        elif int(status_counts.get("failed") or 0) > 0:
            active_action = "inspect_failures"
            phase = "tune"
        elif total_tasks and int(status_counts.get("completed") or 0) == total_tasks:
            active_action = "verify_outputs"
            phase = "verify"
        elif total_tasks:
            active_action = "await_next_stage"
            phase = "observe"

        running_bot_labels = [str(item.get("bot") or item.get("bot_id") or "").strip() for item in active_tasks if str(item.get("status") or "") == "running"]
        running_bot_labels = [item for item in running_bot_labels if item]
        if not context.get("orchestration_id"):
            detail = "No assignment/orchestration attached yet. Attach a target run or start an isolated pipeline test."
            heartbeat_detail = "Waiting for attached assignment/orchestration."
        elif not total_tasks:
            detail = f"Attached to orchestration {context.get('orchestration_id')}, waiting for tasks to appear."
            heartbeat_detail = "No tasks available yet for the attached orchestration."
        else:
            detail = (
                f"Tracking {total_tasks} tasks: running={int(status_counts.get('running') or 0)}, "
                f"queued={int(status_counts.get('queued') or 0)}, blocked={int(status_counts.get('blocked') or 0)}, "
                f"completed={int(status_counts.get('completed') or 0)}, failed={int(status_counts.get('failed') or 0)}."
            )
            if running_bot_labels:
                detail += f" Active bots: {', '.join(running_bot_labels[:4])}."
            heartbeat_detail = f"Monitoring orchestration progress ({completed_like}/{total_tasks} processed tasks)."

        runtime_state = {
            "assignment_id": context.get("assignment_id"),
            "run_id": context.get("run_id"),
            "orchestration_id": context.get("orchestration_id"),
            "pipeline_bot_id": context.get("pipeline_bot_id"),
            "pipeline_name": context.get("pipeline_name"),
            "status_counts": status_counts,
            "task_total": total_tasks,
            "graph_node_total": len(nodes),
            "progress_ratio": round(progress_ratio, 4),
            "active_tasks": active_tasks,
            "focus_nodes": focus_nodes,
            "latest_task": latest_task,
        }
        signature_payload = {
            "assignment_id": runtime_state.get("assignment_id"),
            "run_id": runtime_state.get("run_id"),
            "orchestration_id": runtime_state.get("orchestration_id"),
            "status_counts": runtime_state.get("status_counts"),
            "active_tasks": [
                {
                    "task_id": str(item.get("task_id") or ""),
                    "status": str(item.get("status") or ""),
                    "bot_id": str(item.get("bot_id") or ""),
                    "updated_at": str(item.get("updated_at") or ""),
                }
                for item in active_tasks
            ],
            "latest_task": {
                "task_id": str((latest_task or {}).get("task_id") or ""),
                "status": str((latest_task or {}).get("status") or ""),
                "updated_at": str((latest_task or {}).get("updated_at") or ""),
            },
        }
        signature = json.dumps(signature_payload, sort_keys=True, ensure_ascii=False)
        tick = int((session.get("metadata") or {}).get("runtime_tick") or 0) + 1
        return {
            "signature": signature,
            "tick": tick,
            "phase": phase,
            "active_action": active_action,
            "detail": detail,
            "heartbeat_detail": heartbeat_detail,
            "runtime_state": runtime_state,
            "status_counts": status_counts,
            "active_tasks": active_tasks,
            "orchestration_id": context.get("orchestration_id"),
        }

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
