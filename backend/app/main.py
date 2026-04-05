import logging
import time
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from starlette.middleware.base import BaseHTTPMiddleware
from app.config import settings
from app.routers import agents, skills, chat, files, telegram, logs, cron
from app.logging_config import setup_logging, request_id_var

setup_logging(log_level=settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log startup configuration (redact secrets)
    logger.info(
        "OpenCopilot starting",
        extra={
            "workspace_dir": settings.workspace_dir,
            "auth_enabled": settings.auth_enabled,
            "cors_origins": settings.cors_origins,
            "model": settings.copilot_model,
            "telegram_configured": bool(settings.telegram_bot_token),
            "blob_storage_configured": bool(settings.azure_storage_connection_string),
        },
    )
    # Startup: discover available models from Copilot CLI
    try:
        from app.services.copilot import discover_models
        models = await discover_models()
        logger.info("Discovered %d model groups at startup", len(models))
    except Exception:
        logger.exception("Model discovery failed at startup, using defaults")
    yield
    logger.info("OpenCopilot shutting down")


app = FastAPI(
    title="OpenCopilot API",
    description="Run GitHub Copilot agents and commands via REST/WebSocket API",
    version="1.0.0",
    lifespan=lifespan,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials="*" not in settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


class _RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Assigns a unique request ID and logs each request with timing."""

    async def dispatch(self, request: Request, call_next):
        req_id = request.headers.get("X-Request-ID", uuid.uuid4().hex[:12])
        request_id_var.set(req_id)

        start = time.monotonic()
        response = None
        try:
            response = await call_next(request)
            return response
        except Exception:
            logger.exception(
                "Unhandled request error",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": 500,
                    "duration_ms": round((time.monotonic() - start) * 1000),
                },
            )
            raise
        finally:
            status = response.status_code if response else 500
            duration = round((time.monotonic() - start) * 1000)
            # Skip noisy health checks and static files
            path = request.url.path
            if path not in ("/api/health",) and not path.startswith("/css/") and not path.startswith("/js/"):
                logger.info(
                    "%s %s %d %dms",
                    request.method, path, status, duration,
                    extra={
                        "method": request.method,
                        "path": path,
                        "status_code": status,
                        "duration_ms": duration,
                    },
                )
            if response:
                response.headers["X-Request-ID"] = req_id


app.add_middleware(_SecurityHeadersMiddleware)
app.add_middleware(_RequestLoggingMiddleware)

@app.get("/api/health")
async def health():
    return {"status": "ok"}

@app.get("/api/models")
async def list_models():
    from app.services.copilot import get_models
    return get_models()

app.include_router(agents.router)
app.include_router(skills.router)
app.include_router(chat.router)
app.include_router(files.router)
app.include_router(telegram.router)
app.include_router(logs.router)
app.include_router(cron.router)

# Serve frontend static files (must be last — catches all unmatched routes)
frontend_dir = Path(__file__).parent.parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
