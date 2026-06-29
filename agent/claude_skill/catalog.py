"""claude-skill 目录：从 skills_data/ 加载技能包（SKILL.md：frontmatter + 指令体）。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_SKILLS_DIR = Path(__file__).parent / "skills_data"


@dataclass
class ClaudeSkill:
    skill_id: str
    name: str
    description: str
    instruction: str


def _parse_skill_md(text: str) -> tuple[dict[str, str], str]:
    meta: dict[str, str] = {}
    body = text.strip()
    if text.lstrip().startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip()
            body = parts[2].strip()
    return meta, body


def load_claude_skills(skills_dir: Path = _SKILLS_DIR) -> list[ClaudeSkill]:
    skills: list[ClaudeSkill] = []
    if not skills_dir.exists():
        return skills
    for sub in sorted(skills_dir.iterdir()):
        skill_md = sub / "SKILL.md"
        if not (sub.is_dir() and skill_md.exists()):
            continue
        meta, body = _parse_skill_md(skill_md.read_text(encoding="utf-8"))
        skills.append(ClaudeSkill(
            skill_id=sub.name,
            name=meta.get("name", sub.name),
            description=meta.get("description", ""),
            instruction=body,
        ))
    return skills
