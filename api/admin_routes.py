"""Local admin UI routes and APIs."""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from config.paths import config_dir_path, server_log_path
from config.settings import Settings
from config.settings import get_settings as get_cached_settings
from providers.registry import ProviderRegistry

from .admin_config import (
    FIELD_BY_KEY,
    load_config_response,
    provider_config_status,
    validate_updates,
    write_managed_env,
)
from .admin_urls import local_admin_url

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "admin_static"
LOCAL_PROVIDER_PATHS = {
    "lmstudio": "/models",
    "llamacpp": "/models",
    "ollama": "/api/tags",
}


class AdminConfigPayload(BaseModel):
    """Partial config update submitted by the admin UI."""

    values: dict[str, Any] = Field(default_factory=dict)


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _origin_is_local(origin: str | None) -> bool:
    if not origin:
        return True
    parsed = urlsplit(origin)
    return _is_loopback_host(parsed.hostname)


def require_loopback_admin(request: Request) -> None:
    """Allow admin access only from the local machine."""

    client_host = request.client.host if request.client else None
    if not _is_loopback_host(client_host):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")

    origin = request.headers.get("origin")
    if not _origin_is_local(origin):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")


def _asset_response(filename: str) -> FileResponse:
    path = STATIC_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return FileResponse(path)


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    require_loopback_admin(request)
    return _asset_response("index.html")


@router.get("/admin/assets/{filename}", include_in_schema=False)
async def admin_asset(filename: str, request: Request):
    require_loopback_admin(request)
    if filename not in {"admin.css", "admin.js"}:
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return _asset_response(filename)


@router.get("/admin/api/config")
async def get_admin_config(request: Request):
    require_loopback_admin(request)
    return load_config_response()


@router.post("/admin/api/config/validate")
async def validate_admin_config(payload: AdminConfigPayload, request: Request):
    require_loopback_admin(request)
    return validate_updates(_filtered_values(payload.values))


@router.post("/admin/api/config/apply")
async def apply_admin_config(
    payload: AdminConfigPayload,
    request: Request,
    background_tasks: BackgroundTasks,
):
    require_loopback_admin(request)
    result = write_managed_env(_filtered_values(payload.values))
    if not result["applied"]:
        return result

    get_cached_settings.cache_clear()
    restart = _restart_metadata(result["pending_fields"], request)
    result["restart"] = restart
    if restart["required"] and restart["automatic"]:
        callback = request.app.state.admin_restart_callback
        background_tasks.add_task(_invoke_admin_restart_callback, callback)
        request.app.state.admin_pending_fields = []
        return result

    old_registry = getattr(request.app.state, "provider_registry", None)
    if isinstance(old_registry, ProviderRegistry):
        await old_registry.cleanup()
    request.app.state.provider_registry = ProviderRegistry()
    request.app.state.admin_pending_fields = result["pending_fields"]
    return result


@router.get("/admin/api/status")
async def admin_status(request: Request):
    require_loopback_admin(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    cached_models: dict[str, list[str]] = {}
    if isinstance(registry, ProviderRegistry):
        cached_models = {
            provider_id: sorted(model_ids)
            for provider_id, model_ids in registry.cached_model_ids().items()
        }
    return {
        "status": "running",
        "host": settings.host,
        "port": settings.port,
        "model": settings.model,
        "provider": settings.provider_type,
        "pending_fields": getattr(request.app.state, "admin_pending_fields", []),
        "provider_status": provider_config_status(),
        "cached_models": cached_models,
    }


@router.get("/admin/api/providers/local-status")
async def local_provider_status(request: Request):
    require_loopback_admin(request)
    config = load_config_response()
    values = {field["key"]: field["value"] for field in config["fields"]}
    checks = []
    for provider_id, path in LOCAL_PROVIDER_PATHS.items():
        base_url = _local_provider_url(provider_id, values)
        checks.append(await _check_local_provider(provider_id, base_url, path))
    return {"providers": checks}


@router.post("/admin/api/providers/{provider_id}/test")
async def test_provider(provider_id: str, request: Request):
    require_loopback_admin(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    if not isinstance(registry, ProviderRegistry):
        registry = ProviderRegistry()
        request.app.state.provider_registry = registry
    start_time = time.perf_counter()
    try:
        provider = registry.get(provider_id, settings)
        infos = await provider.list_model_infos()
        latency_ms = int((time.perf_counter() - start_time) * 1000)
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        return {
            "provider_id": provider_id,
            "ok": False,
            "latency_ms": latency_ms,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
    registry.cache_model_infos(provider_id, infos)
    return {
        "provider_id": provider_id,
        "ok": True,
        "latency_ms": latency_ms,
        "models": sorted(info.model_id for info in infos),
    }


@router.get("/admin/api/providers/{provider_id}/models")
async def get_provider_models(provider_id: str, request: Request):
    require_loopback_admin(request)
    registry = getattr(request.app.state, "provider_registry", None)
    if not isinstance(registry, ProviderRegistry):
        return {"models": []}
    cached = registry.cached_model_ids().get(provider_id, [])
    return {"models": sorted(cached)}


@router.get("/admin/api/config/export")
async def export_config(request: Request):
    require_loopback_admin(request)
    config = load_config_response()
    values = {field["key"]: field["value"] for field in config["fields"]}
    return JSONResponse(
        content=values,
        headers={"Content-Disposition": "attachment; filename=fcc-config.json"},
    )


@router.post("/admin/api/config/import")
async def import_config(payload: AdminConfigPayload, request: Request):
    require_loopback_admin(request)
    filtered = _filtered_values(payload.values)
    validation_res = validate_updates(filtered)
    if not validation_res.get("valid", True):
        return {"applied": False, "errors": validation_res.get("errors", [])}
    result = write_managed_env(filtered)
    if not result["applied"]:
        return result
    get_cached_settings.cache_clear()
    restart = _restart_metadata(result["pending_fields"], request)
    result["restart"] = restart
    return result


@router.get("/admin/api/logs/stream")
async def stream_logs(request: Request):
    require_loopback_admin(request)

    async def log_generator():
        log_path = Path(os.getenv("LOG_FILE", server_log_path()))
        if not log_path.is_file():
            yield "data: [No log file found]\n\n"
            return
        try:
            with open(log_path, encoding="utf-8", errors="ignore") as f:
                # Seek to near the end
                f.seek(0, 2)
                file_size = f.tell()
                offset = max(0, file_size - 10240)
                f.seek(offset)
                lines = f.readlines()
                if offset > 0 and lines:
                    lines.pop(0)
                for line in lines:
                    yield f"data: {line.strip()}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    line = f.readline()
                    if not line:
                        await asyncio.sleep(0.5)
                        continue
                    yield f"data: {line.strip()}\n\n"
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            yield f"data: Error reading logs: {exc}\n\n"

    return StreamingResponse(log_generator(), media_type="text/event-stream")


@router.get("/admin/api/profiles")
async def list_profiles(request: Request):
    require_loopback_admin(request)
    config_dir = config_dir_path()
    profiles_dir = config_dir / "profiles"
    profiles = ["default"]
    if profiles_dir.is_dir():
        profiles.extend(f.stem for f in profiles_dir.glob("*.env"))
    return {"profiles": profiles, "active": os.environ.get("ACTIVE_PROFILE", "default")}


@router.post("/admin/api/profiles/switch")
async def switch_profile(
    payload: dict[str, str], request: Request, background_tasks: BackgroundTasks
):
    require_loopback_admin(request)
    profile_name = payload.get("profile", "default").strip()
    config_dir = config_dir_path()
    profile_file = config_dir / "active_profile.txt"
    config_dir.mkdir(parents=True, exist_ok=True)
    if profile_name == "default":
        if profile_file.is_file():
            profile_file.unlink()
        os.environ.pop("ACTIVE_PROFILE", None)
    else:
        profiles_dir = config_dir / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)
        profile_path = profiles_dir / f"{profile_name}.env"
        if not profile_path.is_file():
            profile_path.write_text("# FCC Profile Config\n", encoding="utf-8")
        profile_file.write_text(profile_name, encoding="utf-8")
        os.environ["ACTIVE_PROFILE"] = profile_name
    get_cached_settings.cache_clear()
    callback = getattr(request.app.state, "admin_restart_callback", None)
    if callable(callback):
        background_tasks.add_task(_invoke_admin_restart_callback, callback)
    return {"success": True, "active": profile_name}


@router.post("/admin/api/models/refresh")
async def refresh_models(request: Request):
    require_loopback_admin(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    if not isinstance(registry, ProviderRegistry):
        registry = ProviderRegistry()
        request.app.state.provider_registry = registry
    await registry.refresh_model_list_cache(settings)
    return {
        "cached_models": {
            provider_id: sorted(model_ids)
            for provider_id, model_ids in registry.cached_model_ids().items()
        }
    }


def _filtered_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key in FIELD_BY_KEY}


async def _invoke_admin_restart_callback(callback: Any) -> None:
    result = callback()
    if inspect.isawaitable(result):
        await result


def _restart_metadata(fields: list[str], request: Request) -> dict[str, Any]:
    callback = getattr(request.app.state, "admin_restart_callback", None)
    automatic = bool(fields and callable(callback))
    return {
        "required": bool(fields),
        "automatic": automatic,
        "admin_url": _next_admin_url() if automatic else None,
        "fields": fields,
    }


def _next_admin_url() -> str:
    fields = {
        field["key"]: field["value"] for field in load_config_response()["fields"]
    }
    settings = Settings.model_construct(
        host=fields.get("HOST") or "0.0.0.0",
        port=int(fields.get("PORT") or 8082),
    )
    return local_admin_url(settings)


def _local_provider_url(provider_id: str, values: dict[str, str]) -> str:
    if provider_id == "lmstudio":
        return values.get("LM_STUDIO_BASE_URL", "")
    if provider_id == "llamacpp":
        return values.get("LLAMACPP_BASE_URL", "")
    if provider_id == "ollama":
        return values.get("OLLAMA_BASE_URL", "")
    return ""


async def _check_local_provider(
    provider_id: str, base_url: str, path: str
) -> dict[str, Any]:
    clean_url = base_url.strip().rstrip("/")
    if not clean_url:
        return {
            "provider_id": provider_id,
            "status": "missing_url",
            "label": "Missing URL",
            "base_url": base_url,
        }

    url = f"{clean_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            response = await client.get(url)
        ok = 200 <= response.status_code < 300
        return {
            "provider_id": provider_id,
            "status": "reachable" if ok else "offline",
            "label": "Reachable" if ok else "Offline",
            "base_url": base_url,
            "status_code": response.status_code,
        }
    except Exception as exc:
        return {
            "provider_id": provider_id,
            "status": "offline",
            "label": "Offline",
            "base_url": base_url,
            "error_type": type(exc).__name__,
        }
