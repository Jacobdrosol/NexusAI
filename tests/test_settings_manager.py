def test_task_retry_max_tokens_increment_default_is_disabled() -> None:
    from shared.settings_manager import _DEFAULTS

    defaults = {str(key): str(value) for key, value, *_rest in _DEFAULTS}

    assert defaults["task_retry_max_tokens_increment"] == "0"
