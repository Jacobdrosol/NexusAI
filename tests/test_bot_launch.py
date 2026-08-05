from dashboard.bot_launch import launchable_bots
from dashboard.bot_launch_visibility import blocked_launch_bot_ids


def test_launchable_bots_skip_disabled_bots():
    rows = launchable_bots(
        [
            {
                "id": "disabled-launch",
                "name": "Disabled Launch",
                "enabled": False,
                "routing_rules": {
                    "launch_profile": {
                        "enabled": True,
                        "label": "Disabled Launch",
                        "payload": {"task": "skip"},
                    }
                },
            },
            {
                "id": "ready-launch",
                "name": "Ready Launch",
                "enabled": True,
                "routing_rules": {
                    "launch_profile": {
                        "enabled": True,
                        "label": "Ready Launch",
                        "payload": {"task": "run"},
                    }
                },
            },
        ],
        surface="tasks",
    )

    assert [row["id"] for row in rows] == ["ready-launch"]


def test_launchable_bots_respect_surface_visibility():
    rows = launchable_bots(
        [
            {
                "id": "overview-only",
                "name": "Overview Only",
                "enabled": True,
                "routing_rules": {
                    "launch_profile": {
                        "enabled": True,
                        "label": "Overview Only",
                        "payload": {"task": "run"},
                        "show_on_tasks": False,
                        "show_on_overview": True,
                    }
                },
            }
        ],
        surface="tasks",
    )

    assert rows == []


def test_launchable_bots_skip_known_blocked_bots():
    rows = launchable_bots(
        [
            {
                "id": "blocked-launch",
                "name": "Blocked Launch",
                "enabled": True,
                "routing_rules": {
                    "launch_profile": {
                        "enabled": True,
                        "label": "Blocked Launch",
                        "payload": {"task": "skip"},
                    }
                },
            },
            {
                "id": "ready-launch",
                "name": "Ready Launch",
                "enabled": True,
                "routing_rules": {
                    "launch_profile": {
                        "enabled": True,
                        "label": "Ready Launch",
                        "payload": {"task": "run"},
                    }
                },
            },
        ],
        surface="overview",
        blocked_bot_ids={"blocked-launch"},
    )

    assert [row["id"] for row in rows] == ["ready-launch"]


def test_blocked_launch_bot_ids_uses_tooling_states():
    assert blocked_launch_bot_ids(
        {
            "rows": [
                {"bot_id": "ready-bot", "state": "ready"},
                {"bot_id": "blocked-bot", "state": "blocked"},
                {"bot_id": "disabled-bot", "state": "disabled"},
                {"bot_id": "", "state": "blocked"},
            ]
        }
    ) == {"blocked-bot", "disabled-bot"}
