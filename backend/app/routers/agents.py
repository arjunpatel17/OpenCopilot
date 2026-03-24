from fastapi import APIRouter, Depends, HTTPException, status
from pathlib import Path
from app.auth import get_current_user
from app.config import settings
from app.models.agent import AgentDetail, AgentCreate, AgentUpdate, AgentSummary
from app.services import agent_parser

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _agents_dir() -> Path:
    p = Path(settings.agents_path)
    p.mkdir(parents=True, exist_ok=True)
    return p


@router.get("", response_model=list[AgentSummary])
async def list_agents(user: dict = Depends(get_current_user)):
    return agent_parser.list_agents(_agents_dir())


@router.get("/{name}", response_model=AgentDetail)
async def get_agent(name: str, user: dict = Depends(get_current_user)):
    file_path = _agents_dir() / f"{name}.agent.md"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return agent_parser.load_agent(file_path)


@router.post("", response_model=AgentDetail, status_code=status.HTTP_201_CREATED)
async def create_agent(agent: AgentCreate, user: dict = Depends(get_current_user)):
    file_path = _agents_dir() / f"{agent.name}.agent.md"
    if file_path.exists():
        raise HTTPException(status_code=409, detail=f"Agent '{agent.name}' already exists")
    agent_parser.save_agent(_agents_dir(), agent)
    return agent_parser.load_agent(file_path)


@router.put("/{name}", response_model=AgentDetail)
async def update_agent(name: str, update: AgentUpdate, user: dict = Depends(get_current_user)):
    file_path = _agents_dir() / f"{name}.agent.md"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

    existing = agent_parser.load_agent(file_path)
    updated = AgentCreate(
        name=name,
        description=update.description if update.description is not None else existing.description,
        argument_hint=update.argument_hint if update.argument_hint is not None else existing.argument_hint,
        tools=update.tools if update.tools is not None else existing.tools,
        skills=update.skills if update.skills is not None else existing.skills,
        body=update.body if update.body is not None else existing.body,
    )
    agent_parser.save_agent(_agents_dir(), updated)
    return agent_parser.load_agent(file_path)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(name: str, user: dict = Depends(get_current_user)):
    file_path = _agents_dir() / f"{name}.agent.md"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    file_path.unlink()
