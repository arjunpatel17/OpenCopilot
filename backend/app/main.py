from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from app.config import settings
from app.routers import agents, skills, chat, files, telegram

app = FastAPI(
    title="OpenCopilot API",
    description="Run GitHub Copilot agents and commands via REST/WebSocket API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
async def health():
    return {"status": "ok"}

app.include_router(agents.router)
app.include_router(skills.router)
app.include_router(chat.router)
app.include_router(files.router)
app.include_router(telegram.router)

# Serve frontend static files (must be last — catches all unmatched routes)
frontend_dir = Path(__file__).parent.parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
