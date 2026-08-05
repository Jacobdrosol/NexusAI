from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from control_plane.browser_action_approvals import BrowserActionApprovalStore
from shared.models import BackendConfig, Bot, Capability, Task, Worker


def _question_patch_payload() -> dict:
    return {
        "browser_action": "question_bank",
        "action": "patch_existing",
        "confirmation": "approved:question-bank:patch_existing:42:7",
        "bank_id": 42,
        "question_id": 7,
        "expected": {"prompt": "What is 2 + 2?", "question_type": "MCQ"},
        "changes": {"prompt": "What is 3 + 1?"},
        "review_evidence": {
            "reviewer_bot_id": "question-bank-review",
            "review_task_id": "review-42-7",
            "approved_patch": True,
            "semantic_duplicate_risk": "materially_distinct_context",
            "reviewed_question_ids": [7],
            "shortage_detected": False,
            "rationale": "Reviewed against the bank before approving one exact patch.",
        },
    }


@pytest.mark.anyio
async def test_browser_action_approval_is_payload_bound_and_single_use(tmp_path):
    store = BrowserActionApprovalStore(db_path=str(tmp_path / "approvals.db"))
    payload = _question_patch_payload()
    approval = await store.create(
        bot_id="question-patcher",
        action_key="question_bank.patch_existing",
        payload=payload,
        expires_in_seconds=60,
    )

    assert await store.consume(
        approval_id=approval["id"],
        bot_id="question-patcher",
        action_key="question_bank.patch_existing",
        payload={**payload, "owner_approval_id": approval["id"]},
    )
    assert not await store.consume(
        approval_id=approval["id"],
        bot_id="question-patcher",
        action_key="question_bank.patch_existing",
        payload={**payload, "owner_approval_id": approval["id"]},
    )


@pytest.mark.anyio
async def test_scheduler_requires_and_consumes_owner_approval_before_question_patch(
    monkeypatch, tmp_path
):
    from control_plane.scheduler.scheduler import BackendError, Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="browser", models=["browser-ui"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="browser",
        provider="browser",
        model="browser-ui",
        worker_id=worker.id,
        api_key_ref="BROWSER_WORKER_TOKEN",
    )
    bot = Bot(
        id="question-patcher",
        name="Question Patcher",
        role="question-patcher",
        execution_policy={
            "required_worker_tools": ["browser-ui"],
            "browser_action_allowlist": ["question_bank.patch_existing"],
            "browser_action_owner_approval_required": ["question_bank.patch_existing"],
        },
        backends=[backend],
    )
    task = Task(
        id="task-question-patch",
        bot_id=bot.id,
        payload={},
        created_at="now",
        updated_at="now",
    )
    store = BrowserActionApprovalStore(db_path=str(tmp_path / "approvals.db"))
    payload = _question_patch_payload()
    approval = await store.create(
        bot_id=bot.id,
        action_key="question_bank.patch_existing",
        payload=payload,
        expires_in_seconds=60,
    )
    payload["owner_approval_id"] = approval["id"]
    monkeypatch.setenv("BROWSER_WORKER_TOKEN", "worker-token")
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": "Question Bank patch saved and verified"}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.post.return_value = response
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    bot_registry = AsyncMock()
    bot_registry.get.return_value = bot
    scheduler = Scheduler(
        bot_registry=bot_registry,
        worker_registry=worker_registry,
        browser_action_approval_store=store,
    )

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=client):
        result = await scheduler._dispatch_backend(backend, payload, task=task)
        assert result["status"] == "Question Bank patch saved and verified"
        assert "owner_approval_id" not in client.post.await_args.kwargs["json"]
        with pytest.raises(BackendError, match="valid, unused owner approval"):
            await scheduler._dispatch_backend(backend, payload, task=task)

    assert client.post.await_count == 1


@pytest.mark.anyio
async def test_owner_approval_api_only_issues_for_a_policy_required_action(cp_client):
    bot = {
        "id": "question-patcher",
        "name": "Question Patcher",
        "role": "question-patcher",
        "execution_policy": {
            "required_worker_tools": ["browser-ui"],
            "browser_action_allowlist": ["question_bank.patch_existing"],
            "browser_action_owner_approval_required": ["question_bank.patch_existing"],
        },
        "backends": [],
    }
    create = await cp_client.post("/v1/bots", json=bot)
    assert create.status_code == 200

    response = await cp_client.post(
        "/v1/browser-action-approvals",
        json={
            "bot_id": bot["id"],
            "payload": _question_patch_payload(),
            "expires_in_seconds": 60,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["bot_id"] == bot["id"]
    assert body["action_key"] == "question_bank.patch_existing"
    assert body["approval_id"]


def test_bot_rejects_owner_approval_for_actions_outside_its_allowlist():
    from shared.bot_policy import validate_bot_configuration

    bot = Bot(
        id="unsafe-browser-policy",
        name="Unsafe Browser Policy",
        role="test",
        execution_policy={
            "browser_action_allowlist": ["question_bank.patch_existing"],
            "browser_action_owner_approval_required": ["question_bank.create_one"],
        },
        backends=[],
    )

    assert validate_bot_configuration(bot) == [
        "Bot 'unsafe-browser-policy' requires owner approval for browser actions not present in its "
        "allowlist: question_bank.create_one",
        "Bot 'unsafe-browser-policy' authorizes browser actions but does not require worker tool "
        "'browser-ui'.",
    ]


def test_bot_rejects_browser_actions_without_required_worker_tool():
    from shared.bot_policy import validate_bot_configuration

    bot = Bot(
        id="browser-policy-without-tool",
        name="Browser Policy Without Tool",
        role="test",
        execution_policy={"browser_action_allowlist": ["question_bank.patch_existing"]},
        backends=[],
    )

    assert validate_bot_configuration(bot) == [
        "Bot 'browser-policy-without-tool' authorizes browser actions but does not require worker "
        "tool 'browser-ui'."
    ]
