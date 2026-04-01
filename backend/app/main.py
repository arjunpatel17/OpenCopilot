import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from starlette.middleware.base import BaseHTTPMiddleware
from app.config import settings
from app.routers import agents, skills, chat, files, telegram, logs, cron

logging.basicConfig(level=logging.INFO)
logging.getLogger("app").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: discover available models from Copilot CLI
    try:
        from app.services.copilot import discover_models
        models = await discover_models()
        logger.info("Discovered %d model groups at startup", len(models))
    except Exception:
        logger.exception("Model discovery failed at startup, using defaults")
    yield


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


app.add_middleware(_SecurityHeadersMiddleware)

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
