from pydantic import BaseModel, Field
from typing import Optional


class SkillDetail(BaseModel):
    name: str
    description: str = ""
    body: str = ""
    raw_content: str = ""


class SkillCreate(BaseModel):
    name: str
    description: str = ""
    body: str = ""


class SkillUpdate(BaseModel):
    description: Optional[str] = None
    body: Optional[str] = None


class SkillSummary(BaseModel):
    name: str
    description: str = ""
