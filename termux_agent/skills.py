"""Lazy discovery and conservative parsing for user-invoked Hermes SKILL.md files."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path

MAX_SKILL_CHARS = 48_000
MAX_ACTIVE_SKILL_CHARS = 128_000
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_skill_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("Invalid skill name")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    root: Path


class SkillRegistry:
    def __init__(self, roots=None):
        self.roots = tuple(Path(root).expanduser() for root in (roots or (
            Path.home() / ".hermes/skills", Path.cwd() / "skills",
            Path.home() / ".config/termux-agent/skills")))

    @staticmethod
    def _frontmatter(path: Path) -> tuple[str, str]:
        with path.open("r", encoding="utf-8") as stream:
            text = stream.read(8193)
        match = re.match(r"\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
        if not match:
            return path.parent.name, ""
        name, description, description_lines, in_description = path.parent.name, "", [], False
        for line in match.group(1).splitlines():
            if not line.startswith((" ", "\t")):
                in_description = False
            value = re.match(r"^(name|description):(?:[ \t]*(.*))?$", line)
            if value:
                field, raw = value.groups()
                raw = (raw or "").strip()
                if field == "name":
                    name = SkillRegistry._scalar(raw) or path.parent.name
                else:
                    in_description = raw in ("|", ">", "|-", ">-")
                    if not in_description:
                        description = SkillRegistry._scalar(raw)
            elif in_description and line.startswith((" ", "\t")):
                description_lines.append(line.strip())
        if description_lines:
            description = " ".join(description_lines)
        try:
            validate_skill_name(name)
        except ValueError:
            name = path.parent.name
        return name, description[:1000]

    @staticmethod
    def _scalar(value: str) -> str:
        if value.startswith('"') and value.endswith('"'):
            try:
                return json.loads(value)
            except ValueError:
                return value[1:-1]
        if value.startswith("'") and value.endswith("'"):
            return value[1:-1].replace("''", "'")
        return re.sub(r"\s+#.*$", "", value).strip()

    def list_skills(self) -> list[Skill]:
        found = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("SKILL.md")):
                if any(part.startswith(".") for part in path.relative_to(root).parts):
                    continue
                resolved_root, resolved_path = root.resolve(), path.resolve()
                if not resolved_path.is_relative_to(resolved_root):
                    continue
                try:
                    name, description = self._frontmatter(path)
                except (OSError, UnicodeError):
                    continue
                found.setdefault(name, Skill(name, description, resolved_path, resolved_root))
        return list(found.values())

    def find(self, name: str) -> Skill:
        validate_skill_name(name)
        for skill in self.list_skills():
            if skill.name == name:
                return skill
        raise ValueError(f"Skill not found: {name}")

    def load(self, name: str) -> tuple[Skill, str]:
        skill = self.find(name)
        if not skill.path.is_relative_to(skill.root):
            raise ValueError("Skill path rejected")
        if skill.path.stat().st_size > MAX_SKILL_CHARS * 4:
            raise ValueError(f"Skill exceeds {MAX_SKILL_CHARS} characters")
        with skill.path.open("r", encoding="utf-8") as stream:
            content = stream.read(MAX_SKILL_CHARS + 1)
        if len(content) > MAX_SKILL_CHARS:
            raise ValueError(f"Skill exceeds {MAX_SKILL_CHARS} characters")
        return skill, content
