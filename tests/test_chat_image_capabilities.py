from control_plane.api.chat import _model_supports_image_attachments as cp_supports_images
from dashboard.routes.chat import (
    _effective_model_context_from_cp,
    _model_supports_image_attachments as dashboard_supports_images,
)


class _CatalogCP:
    def list_models(self):
        return [
            {
                "id": "ollama-cloud-glm-5-3-flash",
                "name": "glm-5.3-flash:cloud",
                "provider": "ollama_cloud",
                "capabilities": ["chat", "vision"],
                "enabled": True,
            }
        ]


def test_control_plane_treats_glm53_flash_as_vision_capable():
    assert (
        cp_supports_images(
            provider="ollama_cloud",
            model_name="glm-5.3-flash:cloud",
            capabilities=None,
        )
        is True
    )


def test_dashboard_treats_glm53_flash_as_vision_capable_without_catalog_entry():
    assert (
        dashboard_supports_images(
            provider="ollama_cloud",
            model_name="glm-5.3-flash:cloud",
            capabilities=[],
        )
        is True
    )


def test_dashboard_uses_catalog_capabilities_for_bot_backend():
    context = _effective_model_context_from_cp(
        _CatalogCP(),
        {"default_bot_id": "image-chat-bot", "default_model_id": None},
        {"backends": [{"provider": "ollama_cloud", "model": "glm-5.3-flash:cloud"}]},
        "image-chat-bot",
    )

    assert context["source"] == "bot_backend"
    assert context["capabilities"] == ["chat", "vision"]
    assert context["image_attachments_supported"] is True


def test_text_model_is_not_vision_capable():
    assert (
        dashboard_supports_images(
            provider="ollama_cloud",
            model_name="llama3.1:8b",
            capabilities=["chat"],
        )
        is False
    )
