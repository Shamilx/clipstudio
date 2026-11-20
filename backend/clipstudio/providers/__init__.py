"""LLM providers for highlight detection.

Four adapters behind one interface. ``openai`` is the widest of them: because
its base URL is configurable, it also serves OpenRouter, Groq, DeepSeek,
Together, and any local OpenAI-compatible server.
"""

from __future__ import annotations

import os

from ..config import DEFAULT_NVIDIA_MODEL, Settings, get_secret, nvidia_api_key
from .anthropic_provider import AnthropicProvider
from .base import (
    ClipCandidate,
    ClipCandidates,
    DetectionConfig,
    LLMProvider,
    ProviderError,
    ProviderStatus,
    TranscriptWindow,
)
from .gemini_provider import GeminiProvider
from .ollama_provider import OllamaProvider
from .openai_provider import OpenAIProvider

__all__ = [
    "PROVIDERS",
    "AnthropicProvider",
    "ClipCandidate",
    "ClipCandidates",
    "DetectionConfig",
    "GeminiProvider",
    "LLMProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "ProviderError",
    "ProviderStatus",
    "TranscriptWindow",
    "build_provider",
    "detection_config",
    "provider_names",
]

PROVIDERS: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
}


def provider_names() -> list[str]:
    return list(PROVIDERS)


def build_provider(name: str | None = None, settings: Settings | None = None) -> LLMProvider:
    """Construct a configured provider.

    Pulls the model and base URL from settings and the API key from the keyring,
    so callers never handle secrets themselves.
    """
    from ..config import load

    settings = settings if settings is not None else load()
    key = name or settings.active_provider

    provider_cls = PROVIDERS.get(key)
    if provider_cls is None:
        raise ProviderError(
            f"Unknown provider '{key}'.",
            hint=f"Available providers: {', '.join(PROVIDERS)}",
        )

    provider_settings = settings.provider(key)
    api_key = get_secret(key, settings) if provider_cls.requires_key else None
    model = provider_settings.model

    # NVIDIA's endpoint takes its key from .env (NVIDIA_API_KEY) or the key saved
    # through the first-run setup screen, and its model from NVIDIA_MODEL.
    if key == "openai" and "nvidia.com" in (provider_settings.base_url or ""):
        api_key = nvidia_api_key(settings) or api_key
        model = os.environ.get("NVIDIA_MODEL") or model or DEFAULT_NVIDIA_MODEL

    provider = provider_cls(model, api_key=api_key, base_url=provider_settings.base_url)
    if key == "openai" and "nvidia.com" in (provider_settings.base_url or ""):
        provider.fallback_model = os.environ.get("NVIDIA_FALLBACK_MODEL") or None
        # Groq is far faster than NVIDIA's free tier: when GROQ_LLM_MODEL is set,
        # Groq answers first and NVIDIA becomes the fallback endpoint.
        groq_model = os.environ.get("GROQ_LLM_MODEL")
        groq_key = os.environ.get("GROQ_API_KEY")
        if groq_model and groq_key:
            nvidia_key, nvidia_url = api_key, provider_settings.base_url
            provider = provider_cls(groq_model, api_key=groq_key, base_url=GROQ_BASE_URL)
            provider.fallback_model = (
                os.environ.get("NVIDIA_FALLBACK_MODEL") or model
            )
            provider.fallback_api_key = nvidia_key
            provider.fallback_base_url = nvidia_url
            provider.primary_retries = 2
    return provider


GROQ_BASE_URL = "https://api.groq.com/openai/v1"


def detection_config(settings: Settings | None = None) -> DetectionConfig:
    """Build a :class:`DetectionConfig` from user settings."""
    from ..config import load

    settings = settings if settings is not None else load()
    return DetectionConfig(
        min_duration_s=settings.clips.min_duration_s,
        max_duration_s=settings.clips.max_duration_s,
        max_clips=settings.clips.max_clips,
        heuristic_weight=settings.clips.heuristic_weight,
        quality_threshold=settings.clips.quality_threshold,
        min_keep=settings.clips.min_keep,
        language=settings.whisper.language,
    )
