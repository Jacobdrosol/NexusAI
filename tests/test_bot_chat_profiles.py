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


def test_bot_chat_profile_preserves_explicit_capabilities_without_duplicates():
    profile = bot_chat_profile(
        {
            "id": "research-tutor",
            "name": "Research Tutor",
            "role": "tutor",
            "routing_rules": {
                "chat_profile": {
                    "mode": "tutor",
                    "capabilities": ["physics_reasoning", "engineering_reasoning", "math_reasoning"],
                }
            },
        }
    )

    assert profile["mode"] == "tutor"
    assert profile["capabilities"].count("math_reasoning") == 1
    assert "physics_reasoning" in profile["capabilities"]
    assert "engineering_reasoning" in profile["capabilities"]
    assert "step_by_step_reasoning" in profile["capabilities"]


def test_bot_chat_profile_adds_math_reasoning_to_vision_profiles():
    profile = bot_chat_profile(
        {
            "id": "vision-stem-helper",
            "name": "Vision STEM Helper",
            "role": "assistant",
            "backends": [{"capabilities": ["vision"]}],
        }
    )

    assert profile["mode"] == "vision"
    assert "image_understanding" in profile["capabilities"]
    assert "diagrams" in profile["capabilities"]
    assert "math_reasoning" in profile["capabilities"]
