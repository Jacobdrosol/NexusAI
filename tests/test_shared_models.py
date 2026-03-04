"""Tests for shared Pydantic models."""


def test_worker_model_valid():
    from shared.models import Worker
    w = Worker(id="w1", name="Test Worker", host="localhost", port=8001, capabilities=[])
    assert w.id == "w1"
    assert w.status == "offline"


def test_worker_model_invalid_port():
    from shared.models import Worker
    # Should not raise — port is just an int, no range validation at model level
    w = Worker(id="w1", name="Test", host="localhost", port=99999, capabilities=[])
    assert w.port == 99999


def test_bot_model_valid():
    from shared.models import Bot
    b = Bot(id="bot1", name="Assistant", role="helper", backends=[])
    assert b.id == "bot1"
    assert b.enabled is True
    assert b.backends == []


def test_bot_model_with_backend():
    from shared.models import Bot, BackendConfig
    b = Bot(
        id="bot1",
        name="Assistant",
        role="helper",
        backends=[BackendConfig(type="local_llm", provider="ollama", model="llama3", worker_id="w1")]
    )
    assert len(b.backends) == 1
    assert b.backends[0].provider == "ollama"


def test_task_metadata():
    from shared.models import TaskMetadata
    meta = TaskMetadata(source="test", priority=1)
    assert meta.source == "test"
    assert meta.priority == 1


def test_worker_model_has_enabled_field():
    from shared.models import Worker
    w = Worker(id="w1", name="Test", host="localhost", port=8001, capabilities=[])
    assert w.enabled is True
    w2 = Worker(id="w2", name="Test2", host="localhost", port=8001, capabilities=[], enabled=False)
    assert w2.enabled is False


def test_bot_model_has_routing_rules_field():
    from shared.models import Bot
    b = Bot(id="bot1", name="Assistant", role="helper", backends=[])
    assert b.routing_rules is None
    b2 = Bot(id="bot2", name="Bot2", role="coder", backends=[], routing_rules={"rule": "value"})
    assert b2.routing_rules == {"rule": "value"}


def test_bot_model_has_system_prompt_field():
    from shared.models import Bot
    b = Bot(id="bot1", name="Assistant", role="helper", backends=[])
    assert b.system_prompt is None
    b2 = Bot(id="bot2", name="Bot2", role="coder", backends=[], system_prompt="You are a helpful coder.")
    assert b2.system_prompt == "You are a helpful coder."
