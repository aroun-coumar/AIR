"""Model catalog: a project's declared models and where they run (``.air/models.json``).

    {"$schema": "ar.model_catalog/1.0",
     "models": [
       {"name": "workstation", "provider": "openai_compatible", "model": "qwen2.5:14b",
        "location": "local_host", "data_leaves_device": false,
        "endpoint": "http://localhost:11434/v1"},
       {"name": "claude", "provider": "anthropic", "model": "claude-opus-5-5",
        "location": "cloud", "data_leaves_device": true}]}

API keys are never stored here; a profile may name the environment variable holding one
(``api_key_env``). Robot-hosted models can also be discovered from robot adapters
(:meth:`ModelRouter.register_robot`).
"""

from __future__ import annotations

from collections.abc import Callable

from ...core.errors import SchemaError
from ...core.schema import Contract, contract
from ...runtime.trace import Trace
from .base import ChatProvider, ModelProfile
from .router import ModelRouter

__requirements__ = ("AIR02", "REQ18", "F07")


@contract("model_catalog", version="1.0")
class ModelCatalog(Contract):
    models: tuple[ModelProfile, ...] = ()

    def __post_init__(self) -> None:
        names = [m.name for m in self.models]
        if len(names) != len(set(names)):
            raise SchemaError("duplicate model names in catalog")


def provider_for(profile: ModelProfile) -> ChatProvider:
    if profile.provider == "openai_compatible":
        from .openai_compat import OpenAICompatibleProvider
        return OpenAICompatibleProvider(profile)
    if profile.provider == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(profile)
    raise SchemaError("unknown model provider", provider=profile.provider, model=profile.name)


def build_router(catalog: ModelCatalog, trace: Trace | None = None,
                 factory: Callable[[ModelProfile], ChatProvider] = provider_for) -> ModelRouter:
    router = ModelRouter(trace=trace)
    for profile in catalog.models:
        router.register(factory(profile))
    return router
