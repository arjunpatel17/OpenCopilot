from fastapi import APIRouter, Depends, HTTPException, status
from pathlib import Path
from app.auth import get_current_user
from app.config import settings
from app.models.skill import SkillDetail, SkillCreate, SkillUpdate, SkillSummary
from app.services.agent_parser import parse_markdown_file, build_markdown_file

router = APIRouter(prefix="/api/skills", tags=["skills"])


def _skills_dir() -> Path:
    p = Path(settings.skills_path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_skill(file_path: Path) -> SkillDetail:
    raw = file_path.read_text(encoding="utf-8")
    fm, body = parse_markdown_file(raw)
    # .skill.md files have double extension; stem only strips .md
    name = fm.get("name", file_path.stem.removesuffix(".skill"))
    return SkillDetail(
        name=name,
        description=fm.get("description", ""),
        body=body,
        raw_content=raw,
    )


def _list_skills(skills_dir: Path) -> list[SkillSummary]:
    skills = []
    if not skills_dir.exists():
        return skills
    for f in sorted(skills_dir.glob("*.skill.md")):
        try:
            detail = _load_skill(f)
            skills.append(SkillSummary(name=detail.name, description=detail.description))
        except Exception:
            continue
    return skills


@router.get("", response_model=list[SkillSummary])
async def list_skills(user: dict = Depends(get_current_user)):
    return _list_skills(_skills_dir())


@router.get("/{name}", response_model=SkillDetail)
async def get_skill(name: str, user: dict = Depends(get_current_user)):
    file_path = _skills_dir() / f"{name}.skill.md"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    return _load_skill(file_path)


@router.post("", response_model=SkillDetail, status_code=status.HTTP_201_CREATED)
async def create_skill(skill: SkillCreate, user: dict = Depends(get_current_user)):
    file_path = _skills_dir() / f"{skill.name}.skill.md"
    if file_path.exists():
        raise HTTPException(status_code=409, detail=f"Skill '{skill.name}' already exists")
    fm = {"name": skill.name, "description": skill.description}
    content = build_markdown_file(fm, skill.body)
    file_path.write_text(content, encoding="utf-8")
    return _load_skill(file_path)


@router.put("/{name}", response_model=SkillDetail)
async def update_skill(name: str, update: SkillUpdate, user: dict = Depends(get_current_user)):
    file_path = _skills_dir() / f"{name}.skill.md"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    existing = _load_skill(file_path)
    fm = {
        "name": name,
        "description": update.description if update.description is not None else existing.description,
    }
    body = update.body if update.body is not None else existing.body
    content = build_markdown_file(fm, body)
    file_path.write_text(content, encoding="utf-8")
    return _load_skill(file_path)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(name: str, user: dict = Depends(get_current_user)):
    file_path = _skills_dir() / f"{name}.skill.md"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    file_path.unlink()
