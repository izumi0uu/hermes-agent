"""Custom / Ollama provider profile.

Covers any endpoint registered as provider="custom", including local
Ollama instances and named custom OpenAI-compatible gateways. Key quirks:
  - ollama_num_ctx → extra_body.options.num_ctx (local context window)
  - local Ollama reasoning disabled → extra_body.think = False
  - ai.input.im GLM / DeepSeek reasoning → top-level reasoning_effort
"""

from __future__ import annotations

from urllib.parse import urlparse
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


_INPUT_IM_HOSTS = {"ai.input.im", "input.im"}
_INPUT_IM_REASONING_MODELS = (
    "glm-",
    "z-ai/glm-",
    "deepseek-",
    "deepseek/",
)


def _flat_model_name(model: str | None) -> str:
    return (model or "").strip().rsplit("/", 1)[-1].lower()


def _normalize_reasoning_effort(effort: str) -> str | None:
    effort = (effort or "").strip().lower()
    if effort in {"none", "minimal", "low", "medium", "high"}:
        return effort
    if effort in {"xhigh", "max"}:
        return "max"
    return None


def _is_input_im_reasoning_route(base_url: str | None, model: str | None) -> bool:
    parsed = urlparse((base_url or "").strip())
    host = (parsed.hostname or "").strip().lower()
    if host not in _INPUT_IM_HOSTS:
        return False
    flat = _flat_model_name(model)
    return flat.startswith(("glm-", "deepseek-"))


class CustomProfile(ProviderProfile):
    """Custom provider - Ollama local quirks plus relay-specific reasoning."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        ollama_num_ctx: int | None = None,
        model: str | None = None,
        base_url: str | None = None,
        **ctx: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        # Ollama context window
        if ollama_num_ctx:
            options = extra_body.get("options", {})
            options["num_ctx"] = ollama_num_ctx
            extra_body["options"] = options

        if reasoning_config and isinstance(reasoning_config, dict):
            _enabled = reasoning_config.get("enabled", True)
            _effort = (reasoning_config.get("effort") or "").strip().lower()

            # input-im's GLM / DeepSeek relay honors top-level reasoning_effort,
            # while thinking/think toggles are ignored or inconsistently applied.
            if _is_input_im_reasoning_route(base_url, model):
                normalized = _normalize_reasoning_effort(
                    "none" if _enabled is False else _effort
                )
                if normalized:
                    top_level["reasoning_effort"] = normalized
                return extra_body, top_level

            # Local Ollama-style endpoints still use the binary think flag.
            if _effort == "none" or _enabled is False:
                extra_body["think"] = False

        return extra_body, top_level

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Custom/Ollama: base_url is user-configured; fetch if set."""
        if not (base_url or self.base_url):
            return None
        return super().fetch_models(api_key=api_key, base_url=base_url, timeout=timeout)


custom = CustomProfile(
    name="custom",
    aliases=(
        "ollama",
        "local",
        "vllm",
        "llamacpp",
        "llama.cpp",
        "llama-cpp",
    ),
    env_vars=(),  # No fixed key — custom endpoint
    base_url="",  # User-configured
    # Without this, no max_tokens is sent and Ollama falls back to its internal
    # num_predict=128, truncating responses after a few tokens (#39281). This is
    # only a floor used when the user hasn't set model.max_tokens — they can
    # override per-model — so we set it generously rather than lowballing it.
    default_max_tokens=65536,
)

register_provider(custom)
