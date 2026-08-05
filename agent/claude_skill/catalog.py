"""本地 Claude SKILL 包目录：frontmatter 只用于发现，正文由子 Agent 运行时读取。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agent.claude_skill.contracts import SkillPackageInvalidError

_SKILLS_DIR = Path(__file__).parent / "skills_data"
_SKILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RESOURCE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


@dataclass(frozen=True)
class ClaudeSkill:
    skill_id: str
    name: str
    description: str
    root_dir: Path
    parallel_safe: bool = False
    exclusive_resources: tuple[str, ...] = ()

    @property
    def tool_name(self) -> str:
        return f"claude_skill_{self.skill_id}".replace("-", "_")


def _require_text(meta: dict[str, Any], key: str, source: Path) -> str:
    value = meta.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillPackageInvalidError(f"{source}: frontmatter.{key} 必须是非空字符串")
    return value.strip()


def _parse_skill_md(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SkillPackageInvalidError(f"无法读取 {path}: {type(exc).__name__}") from exc
    normalized = text.removeprefix("\ufeff")
    lines = normalized.splitlines()
    if not lines or lines[0] != "---":
        raise SkillPackageInvalidError(f"{path}: 缺少 YAML frontmatter")
    try:
        closing_index = lines.index("---", 1)
    except ValueError as exc:
        raise SkillPackageInvalidError(f"{path}: frontmatter 或指令正文不完整") from exc
    frontmatter = "\n".join(lines[1:closing_index])
    body = "\n".join(lines[closing_index + 1:])
    if not frontmatter.strip() or not body.strip():
        raise SkillPackageInvalidError(f"{path}: frontmatter 或指令正文不完整")
    try:
        meta = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise SkillPackageInvalidError(f"{path}: frontmatter YAML 非法") from exc
    if not isinstance(meta, dict):
        raise SkillPackageInvalidError(f"{path}: frontmatter 必须是对象")
    return meta


def _parse_resources(value: Any, source: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise SkillPackageInvalidError(f"{source}: exclusive_resources 必须是字符串数组")
    resources = tuple(dict.fromkeys(v.strip() for v in value))
    invalid = [resource for resource in resources if not _RESOURCE_RE.fullmatch(resource)]
    if invalid:
        raise SkillPackageInvalidError(f"{source}: 非法 exclusive_resources={invalid}")
    return resources


def _load_one_skill(root_dir: Path) -> ClaudeSkill:
    skill_id = root_dir.name
    if not _SKILL_ID_RE.fullmatch(skill_id):
        raise SkillPackageInvalidError(f"非法 skill_id={skill_id!r}")
    skill_md = root_dir / "SKILL.md"
    if skill_md.is_symlink() or not skill_md.is_file():
        raise SkillPackageInvalidError(f"技能目录缺少根 SKILL.md: {root_dir}")
    meta = _parse_skill_md(skill_md)
    parallel_safe = meta.get("parallel_safe", False)
    if not isinstance(parallel_safe, bool):
        raise SkillPackageInvalidError(f"{skill_md}: parallel_safe 必须是 boolean")
    skill = ClaudeSkill(
        skill_id=skill_id,
        name=_require_text(meta, "name", skill_md),
        description=_require_text(meta, "description", skill_md),
        root_dir=root_dir.resolve(),
        parallel_safe=parallel_safe,
        exclusive_resources=_parse_resources(meta.get("exclusive_resources"), skill_md),
    )
    if not _TOOL_NAME_RE.fullmatch(skill.tool_name):
        raise SkillPackageInvalidError(f"skill_id={skill_id!r} 生成了非法工具名 {skill.tool_name!r}")
    return skill


def load_claude_skills(skills_dir: Path = _SKILLS_DIR) -> list[ClaudeSkill]:
    """加载并严格校验所有非隐藏技能目录；配置错误直接阻止启动。"""
    if not skills_dir.exists():
        raise SkillPackageInvalidError(f"技能根目录不存在: {skills_dir}")
    if not skills_dir.is_dir():
        raise SkillPackageInvalidError(f"技能根路径不是目录: {skills_dir}")

    entries = [sub for sub in sorted(skills_dir.iterdir()) if not sub.name.startswith(".")]
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            raise SkillPackageInvalidError(f"技能根目录包含非法条目: {entry}")
    skills = [_load_one_skill(sub) for sub in entries]
    tool_names: set[str] = set()
    display_names: set[str] = set()
    for skill in skills:
        if skill.tool_name in tool_names:
            raise SkillPackageInvalidError(f"重复 Claude Skill 工具名: {skill.tool_name}")
        if skill.name in display_names:
            raise SkillPackageInvalidError(f"重复 Claude Skill 展示名: {skill.name}")
        tool_names.add(skill.tool_name)
        display_names.add(skill.name)
    return skills
