#!/usr/bin/env python3
"""
Initialize an OpenCompany skill directory.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MAX_SKILL_NAME_LENGTH = 64
ALLOWED_RESOURCE_DIRS = {"scripts", "references", "assets"}

SKILL_DOC_TEMPLATE = """# {title}

Use this skill when [TODO: describe the trigger clearly].

## Workflow

1. [TODO]
2. [TODO]

## Resources

- Keep helper files under `resources/`.
- Remove this section if the skill does not need extra files.
"""

SKILL_DOC_CN_TEMPLATE = """# {title_cn}

当 [TODO: 明确描述触发场景] 时，使用这个 skill。

## 工作流程

1. [TODO]
2. [TODO]

## 资源

- 辅助文件统一放在 `resources/` 下。
- 如果不需要额外文件，可以删除本节。
"""

REFERENCE_TEMPLATE = """# Reference Notes

Replace this file with the detailed reference material for the skill.
"""

ASSET_README_TEMPLATE = """Place binary assets, templates, or sample files here when the skill needs them.
"""

SCRIPT_TEMPLATE = """#!/usr/bin/env python3
from __future__ import annotations


def main() -> int:
    print("replace this placeholder script with a real helper")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def normalize_skill_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized


def title_case(skill_name: str) -> str:
    return " ".join(part.capitalize() for part in skill_name.split("-") if part)


def toml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_resources(raw: str) -> list[str]:
    resources = parse_csv(raw)
    invalid = sorted({item for item in resources if item not in ALLOWED_RESOURCE_DIRS})
    if invalid:
        allowed = ", ".join(sorted(ALLOWED_RESOURCE_DIRS))
        raise ValueError(f"Unknown resource directories: {', '.join(invalid)}. Allowed: {allowed}")
    deduped: list[str] = []
    for item in resources:
        if item not in deduped:
            deduped.append(item)
    return deduped


def render_skill_toml(
    *,
    skill_id: str,
    name: str,
    name_cn: str,
    description: str,
    description_cn: str,
    tags: list[str],
) -> str:
    rendered_tags = ", ".join(toml_quote(tag) for tag in tags)
    return "\n".join(
        [
            "[skill]",
            f"id = {toml_quote(skill_id)}",
            f"name = {toml_quote(name)}",
            f"name_cn = {toml_quote(name_cn)}",
            f"description = {toml_quote(description)}",
            f"description_cn = {toml_quote(description_cn)}",
            f"tags = [{rendered_tags}]",
            "",
        ]
    )


def write_file(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def create_examples(skill_dir: Path, resource_dirs: list[str]) -> None:
    if "scripts" in resource_dirs:
        write_file(skill_dir / "resources" / "scripts" / "example.py", SCRIPT_TEMPLATE, executable=True)
    if "references" in resource_dirs:
        write_file(skill_dir / "resources" / "references" / "notes.md", REFERENCE_TEMPLATE)
    if "assets" in resource_dirs:
        write_file(skill_dir / "resources" / "assets" / "README.txt", ASSET_README_TEMPLATE)


def init_skill(
    *,
    skill_name: str,
    skills_root: Path,
    resource_dirs: list[str],
    include_cn_doc: bool,
    include_examples: bool,
    description: str,
    description_cn: str,
    tags: list[str],
) -> Path:
    skill_dir = skills_root / skill_name
    if skill_dir.exists():
        raise FileExistsError(f"Skill directory already exists: {skill_dir}")

    skill_dir.mkdir(parents=True, exist_ok=False)
    skill_title = title_case(skill_name)
    name_cn = skill_title
    write_file(
        skill_dir / "skill.toml",
        render_skill_toml(
            skill_id=skill_name,
            name=skill_title,
            name_cn=name_cn,
            description=description,
            description_cn=description_cn,
            tags=tags,
        ),
    )
    write_file(skill_dir / "SKILL.md", SKILL_DOC_TEMPLATE.format(title=skill_title))
    if include_cn_doc:
        write_file(skill_dir / "SKILL_cn.md", SKILL_DOC_CN_TEMPLATE.format(title_cn=name_cn))

    for resource_dir in resource_dirs:
        (skill_dir / "resources" / resource_dir).mkdir(parents=True, exist_ok=True)
    if include_examples:
        create_examples(skill_dir, resource_dirs)
    return skill_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create an OpenCompany-style skill skeleton.")
    parser.add_argument("skill_name", help="Skill name; normalized to hyphen-case")
    parser.add_argument("--path", required=True, help="Path to the skills root directory")
    parser.add_argument(
        "--resources",
        default="",
        help="Comma-separated resource subdirectories to create under resources/: scripts,references,assets",
    )
    parser.add_argument(
        "--cn-doc",
        action="store_true",
        help="Create SKILL_cn.md",
    )
    parser.add_argument(
        "--examples",
        action="store_true",
        help="Create placeholder files inside the selected resource directories",
    )
    parser.add_argument(
        "--description",
        default="Explain when to use this skill and what it helps with.",
        help="English description written into skill.toml",
    )
    parser.add_argument(
        "--description-cn",
        default="说明何时使用该 skill 以及它能帮助完成什么工作。",
        help="Chinese description written into skill.toml",
    )
    parser.add_argument(
        "--tags",
        default="",
        help="Comma-separated skill tags",
    )
    args = parser.parse_args(argv)

    skill_name = normalize_skill_name(args.skill_name)
    if not skill_name:
        print("[ERROR] Skill name must contain at least one letter or digit.", file=sys.stderr)
        return 1
    if len(skill_name) > MAX_SKILL_NAME_LENGTH:
        print(
            f"[ERROR] Skill name is too long ({len(skill_name)} > {MAX_SKILL_NAME_LENGTH}).",
            file=sys.stderr,
        )
        return 1

    try:
        resource_dirs = parse_resources(args.resources)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if args.examples and not resource_dirs:
        print("[ERROR] --examples requires at least one --resources entry.", file=sys.stderr)
        return 1

    tags = parse_csv(args.tags) or ["todo"]
    try:
        skill_dir = init_skill(
            skill_name=skill_name,
            skills_root=Path(args.path).resolve(),
            resource_dirs=resource_dirs,
            include_cn_doc=args.cn_doc,
            include_examples=args.examples,
            description=args.description.strip(),
            description_cn=args.description_cn.strip(),
            tags=tags,
        )
    except Exception as exc:  # pragma: no cover - script surface
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    validator_path = Path(__file__).with_name("quick_validate.py")
    print(f"[OK] Created skill at {skill_dir}")
    print("Next steps:")
    print("1. Refine skill.toml descriptions and tags.")
    print("2. Replace TODOs in SKILL.md (and SKILL_cn.md if created).")
    print("3. Add only the resource files this skill actually needs.")
    print(f"4. Validate with: {validator_path} {skill_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
