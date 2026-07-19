from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    file_path: Path
    content: str

    def prompt_entry(self) -> str:
        return (
            "  <skill>\n"
            f"    <name>{_escape_xml(self.name)}</name>\n"
            f"    <description>{_escape_xml(self.description)}</description>\n"
            f"    <location>{_escape_xml(str(self.file_path))}</location>\n"
            "  </skill>"
        )


class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir

    def load(self) -> list[Skill]:
        if not self.skills_dir.exists():
            return []

        skills = []
        for skill_file in sorted(self.skills_dir.glob("*/SKILL.md")):
            skill = self._load_one(skill_file)
            if skill:
                skills.append(skill)
        return skills

    def build_prompt(self) -> tuple[str, int]:
        skills = self.load()
        if not skills:
            return "", 0

        entries = "\n".join(skill.prompt_entry() for skill in skills)
        prompt = (
            "<available_skills>\n"
            "Use these skills as task-specific operating instructions when relevant. "
            "Read the skill file before following detailed steps.\n"
            f"{entries}\n"
            "</available_skills>"
        )
        return prompt, len(skills)

    def _load_one(self, skill_file: Path) -> Optional[Skill]:
        content = skill_file.read_text(encoding="utf-8")
        metadata, body = _split_frontmatter(content)

        name = metadata.get("name") or skill_file.parent.name
        description = metadata.get("description") or _first_paragraph(body)

        name = name.strip()
        description = description.strip()
        if not name or not description:
            return None

        return Skill(
            name=name,
            description=description,
            file_path=skill_file.resolve(),
            content=content,
        )


def _split_frontmatter(content: str) -> tuple[dict[str, str], str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content

    metadata = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body = "\n".join(lines[index + 1:])
            return metadata, body
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip('"').strip("'")

    return {}, content


def _first_paragraph(content: str) -> str:
    lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            if lines:
                break
            continue
        if line.startswith("#"):
            continue
        lines.append(line)
    return " ".join(lines)


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
