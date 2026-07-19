from types import SimpleNamespace

import pytest

from control_plane.api.orchestration import (
    CancelOrchestrationRequest,
    CompileRunContractRequest,
    CreateBindingRequest,
    cancel_orchestration_run,
    compile_run_contract,
    create_binding,
)


def _request(*, template_store=None, run_store=None):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                orchestration_template_store=template_store,
                orchestration_run_store=run_store,
            )
        )
    )


class _TemplateStore:
    def __init__(self):
        self.create_binding_args = None
        self.compile_args = None

    async def get_template(self, template_id):
        return {"id": template_id, "name": "Course Delivery", "allowed_override_fields": ["mode"]}

    async def create_binding(self, **kwargs):
        self.create_binding_args = kwargs
        return {"id": "binding-1", **kwargs}

    async def get_binding(self, binding_id):
        return {"id": binding_id, "template_id": "template-1", "role_map": {"writer": "writer-bot"}}

    def compile_run_contract(self, **kwargs):
        self.compile_args = kwargs
        return {"binding_id": kwargs["binding"]["id"], "stage_roles": ["writer"]}


@pytest.mark.anyio
async def test_create_binding_maps_api_contract_to_template_store():
    store = _TemplateStore()
    result = await create_binding(
        _request(template_store=store),
        CreateBindingRequest(
            template_id="template-1",
            owner_id="owner-1",
            role_map={"writer": "writer-bot"},
            overrides={"writer": {"mode": "draft"}},
        ),
    )

    assert result["binding"]["id"] == "binding-1"
    assert store.create_binding_args == {
        "template_id": "template-1",
        "owner_id": "owner-1",
        "name": "Course Delivery binding",
        "description": "",
        "role_map": {"writer": "writer-bot"},
        "default_stage_configs": {"writer": {"mode": "draft"}},
        "default_connection_requirements": [],
        "default_context_requirements": [],
        "metadata": {},
    }


@pytest.mark.anyio
async def test_compile_run_contract_is_a_dry_run_against_resolved_binding_and_template():
    store = _TemplateStore()
    result = await compile_run_contract(
        _request(template_store=store),
        CompileRunContractRequest(
            binding_id="binding-1",
            overrides={"stage_overrides": {"writer": {"mode": "review"}}},
            assignment_text="Prepare a course outline.",
            operator_brief="Do not publish.",
        ),
    )

    assert result["contract"] == {"binding_id": "binding-1", "stage_roles": ["writer"]}
    assert store.compile_args["assignment_text"] == "Prepare a course outline."
    assert store.compile_args["operator_brief"] == "Do not publish."


class _RunStore:
    def __init__(self):
        self.cancel_args = None

    async def get_run(self, run_id):
        return {"id": run_id}

    async def cancel_orchestration(self, run_id, *, reason, actor):
        self.cancel_args = {"run_id": run_id, "reason": reason, "actor": actor}
        return {"id": run_id, "orch_state": "cancelled"}


@pytest.mark.anyio
async def test_cancel_orchestration_passes_operator_as_run_store_actor():
    store = _RunStore()
    result = await cancel_orchestration_run(
        "run-1",
        _request(run_store=store),
        CancelOrchestrationRequest(reason="operator stop", operator_id="owner-1"),
    )

    assert result["cancelled"] is True
    assert store.cancel_args == {"run_id": "run-1", "reason": "operator stop", "actor": "owner-1"}
