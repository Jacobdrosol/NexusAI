from __future__ import annotations

from shared.bot_policy import validate_bot_configuration
from shared.models import Bot


def test_bot_rejects_documentation_actions_without_required_worker_tool():
    bot = Bot(
        id="docs-policy-without-tool",
        name="Docs Policy Without Tool",
        role="test",
        execution_policy={"documentation_action_allowlist": ["documentation.create"]},
        backends=[],
    )

    assert validate_bot_configuration(bot) == [
        "Bot 'docs-policy-without-tool' authorizes documentation actions but does not require "
        "worker tool 'documentation-v1'."
    ]


def test_connection_actions_do_not_require_worker_tools():
    bot = Bot(
        id="connection-policy",
        name="Connection Policy",
        role="test",
        execution_policy={
            "connection_action_allowlist": ["acme-agent-api.updatecourse"],
            "connection_action_owner_approval_required": ["acme-agent-api.updatecourse"],
        },
        backends=[],
    )

    assert validate_bot_configuration(bot) == []
