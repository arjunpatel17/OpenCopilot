from pydantic import BaseModel, Field
from typing import Optional


class AgentFrontmatter(BaseModel):
    name: str
    description: str = ""
    argument_hint: str = Field("", alias="argument-hint")
    tools: list[str] = []
    skills: list[str] = []

    model_config = {"populate_by_name": True}


class AgentDetail(BaseModel):
    name: str
    description: str = ""
    argument_hint: str = ""
    tools: list[str] = []
    skills: list[str] = []
    body: str = ""
    raw_content: str = ""


class AgentCreate(BaseModel):
    name: str
    description: str = ""
    argument_hint: str = ""
    tools: list[str] = Field(default_factory=lambda: ["edit", "agent", "search", "web"])
    skills: list[str] = []
    body: str = ""


class AgentUpdate(BaseModel):
    description: Optional[str] = None
    argument_hint: Optional[str] = None
    tools: Optional[list[str]] = None
    skills: Optional[list[str]] = None
    body: Optional[str] = None


class AgentSummary(BaseModel):
    name: str
    description: str = ""
    skills_count: int = 0
