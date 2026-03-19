"""Tests for model-aware context limits."""
import pytest
from shared.settings_manager import _DEFAULTS, get_context_limits_for_model


def test_context_limit_defaults_exist():
    """Test that new context limit defaults are registered."""
    defaults = {str(key): str(value) for key, value, *_rest in _DEFAULTS}
    
    assert "context_item_limit_default" in defaults
    assert defaults["context_item_limit_default"] == "30"
    assert "context_source_limit_default" in defaults
    assert defaults["context_source_limit_default"] == "12"
    assert "context_item_limit_large" in defaults
    assert defaults["context_item_limit_large"] == "100"
    assert "context_source_limit_large" in defaults
    assert defaults["context_source_limit_large"] == "50"
    assert "large_context_model_patterns" in defaults
    assert "gpt-oss" in defaults["large_context_model_patterns"]
    assert "coding_enhancement_enabled" in defaults
    assert defaults["coding_enhancement_enabled"] == "true"
    assert "agent_session_ttl_minutes" in defaults
    assert defaults["agent_session_ttl_minutes"] == "60"


def test_get_context_limits_defaults_unknown_model():
    """Test that unknown models get default limits."""
    # Without a settings instance, it should create one
    # For this test, we'll just verify the logic works with no settings
    # by checking the function returns expected values for unknown models
    item_limit, source_limit = get_context_limits_for_model("unknown-model")
    # Should use defaults: 30, 12
    assert item_limit == 30
    assert source_limit == 12


def test_get_context_limits_large_context_model_patterns():
    """Test that large context models are detected correctly."""
    # Test default patterns: gpt-oss,qwen3.5,claude-3,gpt-4,o1,o3
    item_limit, source_limit = get_context_limits_for_model("gpt-oss:120b-cloud")
    assert item_limit == 100
    assert source_limit == 50
    
    item_limit, source_limit = get_context_limits_for_model("qwen3.5:397b-cloud")
    assert item_limit == 100
    assert source_limit == 50
    
    item_limit, source_limit = get_context_limits_for_model("claude-3-opus")
    assert item_limit == 100
    assert source_limit == 50
    
    # Case insensitive
    item_limit, source_limit = get_context_limits_for_model("GPT-OSS:120B-CLOUD")
    assert item_limit == 100
    assert source_limit == 50


def test_context_limits_categories():
    """Test that new settings are in the correct categories."""
    context_settings = [(key, value, cat) for key, value, vtype, cat, *_rest in _DEFAULTS if cat == "context"]
    coding_settings = [(key, value, cat) for key, value, vtype, cat, *_rest in _DEFAULTS if cat == "coding"]
    
    # Context category should have the limit settings
    context_keys = [s[0] for s in context_settings]
    assert "context_item_limit_default" in context_keys
    assert "context_source_limit_default" in context_keys
    assert "context_item_limit_large" in context_keys
    assert "context_source_limit_large" in context_keys
    assert "large_context_model_patterns" in context_keys
    
    # Coding category should have coding enhancement and session settings
    coding_keys = [s[0] for s in coding_settings]
    assert "coding_enhancement_enabled" in coding_keys
    assert "agent_session_ttl_minutes" in coding_keys