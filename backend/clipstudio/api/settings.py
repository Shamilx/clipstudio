"""Settings, secrets, provider health, and system status."""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, HTTPException

from .. import config, models, system
from ..providers import PROVIDERS, build_provider
from ..providers.base import ProviderStatus
from .schemas import (
    ProviderStatusOut,
    SecretIn,
    SettingsIn,
    SettingsOut,
    SetupStatusOut,
    SetupStepOut,
    SystemOut,
    ValidateKeyIn,
    ValidateKeyOut,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["settings"])


def _settings_out(settings: config.Settings) -> SettingsOut:
    payload = settings.model_dump(mode="json")
    return SettingsOut(
        active_provider=payload["active_provider"],
        providers=payload["providers"],
        whisper=payload["whisper"],
        clips=payload["clips"],
        ingest=payload["ingest"],
        export=payload["export"],
        edit=payload["edit"],
        light=payload["light"],
        insecure_secret_storage=payload["insecure_secret_storage"],
        keys_present={
            name: config.get_secret(name, settings) is not None for name in config.KEYED_PROVIDERS
        }
        | {config.GROQ_KEY: config.get_secret(config.GROQ_KEY, settings) is not None}
        | {config.NVIDIA_KEY: bool(config.nvidia_api_key(settings))}
        | {config.HF_TOKEN_KEY: config.get_secret(config.HF_TOKEN_KEY, settings) is not None},
    )


@router.get("/settings", response_model=SettingsOut)
async def get_settings() -> SettingsOut:
    return _settings_out(config.load())


@router.put("/settings", response_model=SettingsOut)
async def put_settings(payload: SettingsIn) -> SettingsOut:
    """Merge a partial settings update and persist it."""
    settings = config.load()
    updates = payload.model_dump(exclude_none=True)

    if "active_provider" in updates:
        if updates["active_provider"] not in PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown provider. Available: {', '.join(PROVIDERS)}",
            )
        settings.active_provider = updates["active_provider"]

    for section in ("whisper", "clips", "ingest", "export", "edit", "light"):
        if section in updates:
            current = getattr(settings, section)
            try:
                setattr(
                    settings,
                    section,
                    current.model_copy(update=updates[section]).model_validate(
                        current.model_dump() | updates[section]
                    ),
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail=f"Invalid {section} settings: {exc}"
                ) from exc

    if "providers" in updates:
        for name, values in updates["providers"].items():
            provider_settings = settings.provider(name)
            settings.providers[name] = provider_settings.model_copy(update=values)

    if settings.clips.min_duration_s >= settings.clips.max_duration_s:
        raise HTTPException(
            status_code=400, detail="Minimum clip length must be below the maximum."
        )

    config.save(settings)
    return _settings_out(settings)


@router.put("/settings/secrets", status_code=204)
async def put_secret(payload: SecretIn) -> None:
    """Store an API key or token. Values are write-only — never read back."""
    valid = (*config.KEYED_PROVIDERS, config.HF_TOKEN_KEY, config.GROQ_KEY, config.NVIDIA_KEY)
    if payload.key not in valid:
        raise HTTPException(
            status_code=400, detail=f"Unknown secret. Expected one of: {', '.join(valid)}"
        )
    if not payload.value.strip():
        raise HTTPException(status_code=400, detail="The value cannot be empty.")

    config.set_secret(payload.key, payload.value.strip())


@router.delete("/settings/secrets/{key}", status_code=204)
async def delete_secret(key: str) -> None:
    config.delete_secret(key)


@router.get("/providers/status", response_model=list[ProviderStatusOut])
async def providers_status() -> list[ProviderStatusOut]:
    """Live health for every provider, checked concurrently."""
    settings = config.load()

    async def check(name: str) -> ProviderStatusOut:
        provider_cls = PROVIDERS[name]
        has_key = (
            config.get_secret(name, settings) is not None if provider_cls.requires_key else True
        )
        try:
            provider = build_provider(name, settings)
            status: ProviderStatus = await provider.health_check()
        except Exception as exc:
            status = ProviderStatus(name=name, available=False, detail=str(exc)[:200])

        return ProviderStatusOut(
            name=name,
            available=status.available,
            detail=status.detail,
            models=status.models,
            requires_key=provider_cls.requires_key,
            has_key=has_key,
        )

    return list(await asyncio.gather(*(check(name) for name in PROVIDERS)))


@router.get("/system", response_model=SystemOut)
async def system_status() -> SystemOut:
    report = await asyncio.to_thread(system.refresh)
    return SystemOut(
        ready=report.ready,
        python_version=report.python_version,
        platform=report.platform,
        ffmpeg_version=report.ffmpeg.version,
        has_libass=report.ffmpeg.has_libass,
        nvenc_works=report.ffmpeg.nvenc_works,
        accel=report.gpu.accel,
        gpu_name=report.gpu.name,
        compute_type=report.gpu.compute_type,
        diarization_available=report.deps.whisperx,
    )


@router.post("/system/models", status_code=204)
async def fetch_models() -> None:
    """Download any missing ML model bundles."""
    for key in models.MODELS:
        if not models.is_available(key):
            try:
                await asyncio.to_thread(models.ensure, key)
            except models.ModelDownloadError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc


# --------------------------------------------------------------------------
# First-run setup: which keys are needed, and do they work?
# --------------------------------------------------------------------------

GROQ_KEYS_URL = "https://console.groq.com/keys"
NVIDIA_KEYS_URL = "https://build.nvidia.com"
VALIDATE_TIMEOUT_S = 15.0
_MASK = "\u2022\u2022\u2022\u2022"


def _stored(key: str, settings: config.Settings) -> str:
    if key == config.NVIDIA_KEY:
        return config.nvidia_api_key(settings)
    return config.get_secret(key, settings) or ""


def _preview(key: str, settings: config.Settings) -> str | None:
    """Masked tail of a stored key: at most the last four characters."""
    value = _stored(key, settings)
    if not value:
        return None
    return _MASK + value[-4:] if len(value) >= 12 else _MASK


def _is_nvidia_llm(settings: config.Settings) -> bool:
    provider = settings.provider("openai")
    return settings.active_provider == "openai" and "nvidia.com" in (provider.base_url or "")


@router.get("/setup/status", response_model=SetupStatusOut)
async def setup_status() -> SetupStatusOut:
    """Which API keys the current configuration needs, and which are present."""
    settings = config.load()
    steps: list[SetupStepOut] = []

    steps.append(
        SetupStepOut(
            key=config.GROQ_KEY,
            label="Groq",
            purpose="Transcribes the speech in your videos (Whisper).",
            required=settings.whisper.backend == "groq",
            present=bool(_stored(config.GROQ_KEY, settings)),
            preview=_preview(config.GROQ_KEY, settings),
            get_url=GROQ_KEYS_URL,
        )
    )

    active = settings.active_provider
    if _is_nvidia_llm(settings) or active == "ollama":
        # Ollama is local and needs no key, so the NVIDIA step is optional then.
        llm_key, llm_label, llm_url = config.NVIDIA_KEY, "NVIDIA", NVIDIA_KEYS_URL
        llm_required = active != "ollama"
    else:
        llm_key, llm_label, llm_url, llm_required = active, active.capitalize(), "", True
    steps.append(
        SetupStepOut(
            key=llm_key,
            label=llm_label,
            purpose="Reads the transcript and picks the best moments to clip.",
            required=llm_required,
            present=bool(_stored(llm_key, settings)),
            preview=_preview(llm_key, settings),
            get_url=llm_url,
        )
    )
    return SetupStatusOut(ready=all(s.present for s in steps if s.required), steps=steps)


def _probe_request(
    key: str, value: str, settings: config.Settings
) -> tuple[str, str, dict[str, str], dict | None]:
    """Return ``(method, url, headers, json_body)`` for a cheap authenticated call."""
    bearer = {"Authorization": f"Bearer {value}"}
    if key == config.GROQ_KEY:
        return "GET", f"{config.GROQ_BASE_URL}/models", bearer, None
    if key == config.NVIDIA_KEY:
        # /models is publicly readable on NVIDIA's endpoint, so it proves nothing.
        # A one-token chat completion needs a valid key.
        body = {
            "model": "meta/llama-3.1-8b-instruct",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }
        return "POST", f"{config.NVIDIA_BASE_URL}/chat/completions", bearer, body
    if key == "openai":
        base = (settings.provider("openai").base_url or "https://api.openai.com/v1").rstrip("/")
        return "GET", f"{base}/models", bearer, None
    if key == "anthropic":
        headers = {"x-api-key": value, "anthropic-version": "2023-06-01"}
        return "GET", "https://api.anthropic.com/v1/models", headers, None
    if key == "gemini":
        headers = {"x-goog-api-key": value}
        return "GET", "https://generativelanguage.googleapis.com/v1beta/models", headers, None
    raise HTTPException(status_code=400, detail="This key cannot be verified here.")


@router.post("/setup/validate", response_model=ValidateKeyOut)
async def validate_key(payload: ValidateKeyIn) -> ValidateKeyOut:
    """Check a key with one cheap authenticated request. Nothing is stored or logged."""
    settings = config.load()
    value = (payload.value or "").strip() or _stored(payload.key, settings)
    if not value:
        return ValidateKeyOut(ok=False, message="Paste a key first.")

    method, url, headers, body = _probe_request(payload.key, value, settings)
    try:
        async with httpx.AsyncClient(timeout=VALIDATE_TIMEOUT_S) as client:
            response = await client.request(method, url, headers=headers, json=body)
    except httpx.TimeoutException:
        return ValidateKeyOut(ok=False, message="The service did not answer in time. Try again.")
    except httpx.HTTPError as exc:
        # Only the exception type: its text can include the request URL.
        return ValidateKeyOut(
            ok=False, message=f"Could not reach the service ({type(exc).__name__})."
        )

    status = response.status_code
    if status in (401, 403):
        return ValidateKeyOut(
            ok=False, message="The service rejected this key. Check that you copied all of it."
        )
    if status == 429:
        return ValidateKeyOut(
            ok=True, message="Key accepted (the service is rate-limiting right now)."
        )
    if status >= 500:
        return ValidateKeyOut(
            ok=False, message=f"The service had a problem (HTTP {status}). Try again shortly."
        )
    # Auth is checked before the request body, so any other answer means the key works.
    return ValidateKeyOut(ok=True, message="Key works.")
