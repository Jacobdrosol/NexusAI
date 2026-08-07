from dashboard.bot_chat_profiles import bot_chat_profile, bot_direct_chat_access, with_bot_chat_profiles


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


def test_bot_chat_profile_normalizes_disabled_and_mode_less_tool_access():
    disabled = bot_chat_profile(
        {
            "routing_rules": {
                "chat_tool_access": {"enabled": False, "filesystem": True, "repo_search": True},
                "operator_profile": {"autonomy": "manual_chat_only"},
            }
        }
    )
    incomplete = bot_chat_profile(
        {
            "routing_rules": {
                "chat_tool_access": {"enabled": True, "filesystem": False, "repo_search": False},
                "operator_profile": {"autonomy": "manual_chat_only"},
            }
        }
    )

    assert disabled["tool_access"] == {
        "enabled": False,
        "filesystem": False,
        "repo_search": False,
        "web_search": False,
        "mode_error": "",
    }
    assert disabled["tool_label"] == "off"
    assert "filesystem" not in disabled["capabilities"]
    assert "repo_search" not in disabled["capabilities"]
    assert incomplete["tool_access"]["enabled"] is True
    assert incomplete["tool_access"]["mode_error"] == "no enabled tool mode"
    assert incomplete["tool_label"] == "no enabled tool mode"
    assert incomplete["use_label"] == "Tool policy incomplete"


def test_bot_chat_profile_exposes_web_search_without_workspace_access():
    profile = bot_chat_profile(
        {
            "routing_rules": {
                "chat_tool_access": {"enabled": True, "web_search": True},
                "operator_profile": {"autonomy": "manual_chat_only"},
            }
        }
    )

    assert profile["tool_access"]["web_search"] is True
    assert profile["tool_label"] == "web_search"
    assert "web_search" in profile["capabilities"]
    assert profile["use_label"] == "Tool-enabled chat"


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


def test_bot_chat_profile_adds_stem_reasoning_to_vision_profiles():
    profile = bot_chat_profile(
        {
            "id": "vision-stem-helper",
            "name": "Vision STEM Helper",
            "role": "assistant",
            "backends": [{"capabilities": ["vision"]}],
        }
    )

    assert profile["mode"] == "vision"
    assert profile["label"] == "Vision / STEM"
    assert "image_understanding" in profile["capabilities"]
    assert "diagrams" in profile["capabilities"]
    assert "math_reasoning" in profile["capabilities"]
    assert "physics_reasoning" in profile["capabilities"]
    assert "engineering_reasoning" in profile["capabilities"]


def test_bot_chat_profile_labels_project_manager_and_pipeline_use():
    manager = bot_chat_profile(
        {
            "id": "project-manager",
            "name": "Project Manager",
            "assignment_capabilities": {"is_project_manager": True},
        }
    )
    pipeline = bot_chat_profile(
        {
            "id": "pipeline-entry",
            "name": "Pipeline Entry",
            "routing_rules": {"launch_profile": {"is_pipeline": True}},
        }
    )

    assert manager["use_label"] == "Project manager"
    assert pipeline["use_label"] == "Pipeline entry"


def test_bot_chat_profile_labels_manual_and_scheduled_use():
    manual = bot_chat_profile({"routing_rules": {"operator_profile": {"autonomy": "manual_chat_only"}}})
    scheduled = bot_chat_profile({"routing_rules": {"operator_profile": {"autonomy": "scheduled_worker"}}})

    assert manual["use_label"] == "Manual chat"
    assert scheduled["use_label"] == "Scheduled worker"


def test_direct_chat_access_prefers_explicit_bot_configuration():
    disabled = bot_direct_chat_access(
        {"id": "personal-chat", "routing_rules": {"direct_chat": {"enabled": False}}}
    )
    enabled = bot_direct_chat_access(
        {"id": "scheduled-worker", "routing_rules": {"direct_chat": {"enabled": True}}}
    )

    assert disabled == {"enabled": False, "explicit": True}
    assert enabled == {"enabled": True, "explicit": True}
