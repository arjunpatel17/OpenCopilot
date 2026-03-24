import yaml
import re
from pathlib import Path
from app.models.agent import AgentFrontmatter, AgentDetail, AgentCreate, AgentSummary


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


def parse_markdown_file(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter + markdown body from a .agent.md or .skill.md file."""
    match = FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    frontmatter_str, body = match.group(1), match.group(2)
    frontmatter = yaml.safe_load(frontmatter_str) or {}
    return frontmatter, body.strip()


def build_markdown_file(frontmatter: dict, body: str) -> str:
    """Build a .agent.md or .skill.md file from YAML frontmatter + body."""
    fm_str = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return f"---\n{fm_str}---\n\n{body}\n"


def load_agent(file_path: Path) -> AgentDetail:
    """Load an agent from a .agent.md file."""
    raw = file_path.read_text(encoding="utf-8")
    fm, body = parse_markdown_file(raw)
    return AgentDetail(
        name=fm.get("name", file_path.stem.removesuffix(".agent")),
        description=fm.get("description", ""),
        argument_hint=fm.get("argument-hint", ""),
        tools=fm.get("tools", []),
        skills=fm.get("skills", []),
        body=body,
        raw_content=raw,
    )


def save_agent(agents_dir: Path, agent: AgentCreate, body_override: str | None = None) -> Path:
    """Save an agent to a .agent.md file."""
    fm = {
        "name": agent.name,
        "description": agent.description,
        "argument-hint": agent.argument_hint,
        "tools": agent.tools,
        "skills": agent.skills,
    }
    body = body_override if body_override is not None else agent.body
    content = build_markdown_file(fm, body)
    file_path = agents_dir / f"{agent.name}.agent.md"
    file_path.write_text(content, encoding="utf-8")
    return file_path


def list_agents(agents_dir: Path) -> list[AgentSummary]:
    """List all agents in the agents directory."""
    agents = []
    if not agents_dir.exists():
        return agents
    for f in sorted(agents_dir.glob("*.agent.md")):
        try:
            detail = load_agent(f)
            agents.append(AgentSummary(
                name=detail.name,
                description=detail.description,
                skills_count=len(detail.skills),
            ))
        except Exception:
            continue
    return agents
