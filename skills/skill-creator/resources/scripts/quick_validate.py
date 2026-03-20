#!/usr/bin/env python3
"""
Validate that a skill follows the OpenCompany package layout.
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

LEGACY_TOP_LEVEL_DIRS = ("agents", "scripts", "references", "assets")
REQUIRED_METADATA_KEYS = ("id", "name", "name_cn", "description", "description_cn", "tags")


def validate_skill(skill_dir: Path) -> list[str]:
    errors: list[str] = []
    metadata_path = skill_dir / "skill.toml"
    skill_doc_path = skill_dir / "SKILL.md"

    if not metadata_path.is_file():
        errors.append("Missing skill.toml")
        return errors
    if not skill_doc_path.is_file():
        errors.append("Missing SKILL.md")
        return errors

    try:
        payload = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"Failed to parse skill.toml: {exc}")
        return errors

    metadata = payload.get("skill")
    if not isinstance(metadata, dict):
        errors.append("skill.toml must contain a [skill] table")
        return errors

    for key in REQUIRED_METADATA_KEYS:
        if key not in metadata:
            errors.append(f"Missing metadata field: {key}")

    skill_id = str(metadata.get("id", "") or "").strip()
    if skill_id != skill_dir.name:
        errors.append(
            f"Metadata id '{skill_id}' does not match directory name '{skill_dir.name}'"
        )

    for key in ("name", "name_cn", "description", "description_cn"):
        value = metadata.get(key, "")
        if not isinstance(value, str) or not value.strip():
            errors.append(f"Metadata field '{key}' must be a non-empty string")

    tags = metadata.get("tags", [])
    if (
        not isinstance(tags, list)
        or not tags
        or not all(isinstance(item, str) and item.strip() for item in tags)
    ):
        errors.append("Metadata field 'tags' must be a non-empty list of strings")

    skill_doc = skill_doc_path.read_text(encoding="utf-8")
    if skill_doc.lstrip().startswith("---\n"):
        errors.append("SKILL.md still contains legacy YAML frontmatter; move metadata into skill.toml")

    for legacy_name in LEGACY_TOP_LEVEL_DIRS:
        if (skill_dir / legacy_name).exists():
            errors.append(
                f"Legacy top-level '{legacy_name}/' detected; move its contents under resources/{legacy_name}/"
            )

    resources_dir = skill_dir / "resources"
    if resources_dir.exists() and not resources_dir.is_dir():
        errors.append("resources exists but is not a directory")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an OpenCompany skill directory.")
    parser.add_argument("skill_dir", help="Path to the skill directory")
    args = parser.parse_args(argv)

    skill_dir = Path(args.skill_dir).resolve()
    errors = validate_skill(skill_dir)
    if errors:
        for item in errors:
            print(f"[ERROR] {item}")
        return 1
    print("Skill is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
