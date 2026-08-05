from dashboard.bot_chat_profiles import bot_chat_profile, with_bot_chat_profiles


def test_bot_chat_profile_identifies_manual_coding_tool_gates():
    profile = bot_chat_profile(
        {
            "id": "personal-guarded-repo-coder",
            "name": "Guarded Repo Coder",
            "role": "coder",
            "backends": [],
            "routing_rules": {
                "operator_profile": {"autonomy": "manual_chat_only"},
                "chat_tool_access": {"enabled": True, "filesystem": True, "repo_search": True},
            },
            "execution_policy": {"repo_output_mode": "allow", "inline_coding_default": True},
        }
    )

    assert profile["mode"] == "coding"
    assert profile["autonomy"] == "manual_chat_only"
    assert profile["tool_label"] == "filesystem, repo_search"
    assert "filesystem" in profile["capabilities"]
    assert "repo_search" in profile["capabilities"]
    assert "repo_output" in profile["capabilities"]
    assert "inline_coding_default" in profile["capabilities"]


def test_with_bot_chat_profiles_preserves_bot_fields_and_adds_profile():
    rows = with_bot_chat_profiles(
        [
            {
                "id": "personal-vision-math-tutor",
                "name": "STEM Tutor",
                "role": "tutor",
                "backends": [{"capabilities": ["vision"]}],
                "routing_rules": {"chat_profile": {"mode": "vision", "label": "STEM Vision Tutor"}},
            }
        ]
    )

    assert rows[0]["id"] == "personal-vision-math-tutor"
    assert rows[0]["chat_profile"]["label"] == "STEM Vision Tutor"
    assert "image_understanding" in rows[0]["chat_profile"]["capabilities"]
