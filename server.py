"""
Claude Code Proxy - Entry Point

Minimal entry point that builds the ASGI app via :func:`api.app.create_app`.
Run with: uv run uvicorn server:app --host 0.0.0.0 --port 8082 --timeout-graceful-shutdown 5
"""

from api.app import create_app, create_asgi_app

app = create_asgi_app()

__all__ = ["app", "create_app"]

if __name__ == "__main__":
    import os
    import sys

    import uvicorn

    from cli.process_registry import kill_all_best_effort
    from config.settings import get_settings

    settings = get_settings()
    is_testing = "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ
    log_level = "debug" if is_testing else "warning"
    if not is_testing:
        print(
            f"Free Claude Code Proxy starting on http://{settings.host}:{settings.port}"
        )
        print(
            f"Admin UI is available at http://{settings.host}:{settings.port}/admin (local-only)"
        )
    try:
        # timeout_graceful_shutdown ensures uvicorn doesn't hang on task cleanup.
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            log_level=log_level,
            timeout_graceful_shutdown=5,
        )
    finally:
        # Safety net: cleanup subprocesses if lifespan shutdown doesn't fully run.
        kill_all_best_effort()
