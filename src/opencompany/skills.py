from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencompany.utils import hash_bytes

SKILLS_DIRNAME = "skills"
SKILL_BUNDLE_DIRNAME = ".opencompany_skills"
SKILL_MANIFEST_FILENAME = "manifest.json"
SKILL_METADATA_FILENAME = "skill.toml"
SKILL_MAIN_DOC_FILENAME = "SKILL.md"
SKILL_MAIN_DOC_CN_FILENAME = "SKILL_cn.md"
SKILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class SkillFileRecord:
    relative_path: str
    sha256: str
    size: int
    is_binary: bool
    mode: str
    is_executable: bool

    def to_record(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size": self.size,
            "is_binary": self.is_binary,
            "mode": self.mode,
            "is_executable": self.is_executable,
        }


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    id: str
    name: str
    name_cn: str
    description: str
    description_cn: str
    tags: tuple[str, ...]
    source_type: str
    source_root: str
    source_path: str
    main_doc_path: str
    localized_doc_path: str
    files: tuple[SkillFileRecord, ...]

    def to_catalog_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "name_cn": self.name_cn,
            "description": self.description,
            "description_cn": self.description_cn,
            "tags": list(self.tags),
            "source_type": self.source_type,
            "source_root": self.source_root,
            "source_path": self.source_path,
            "main_doc_path": self.main_doc_path,
            "localized_doc_path": self.localized_doc_path,
            "files": [entry.to_record() for entry in self.files],
        }


def normalize_skill_id(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("Skill id is required.")
    if not SKILL_ID_RE.fullmatch(normalized):
        raise ValueError(
            "Invalid skill id. Use letters, numbers, '.', '_', or '-' only."
        )
    return normalized


def normalize_skill_ids(values: list[str] | tuple[str, ...] | None) -> list[str]:
    if not values:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        candidate = normalize_skill_id(raw)
        if candidate in seen:
            continue
        normalized.append(candidate)
        seen.add(candidate)
    return normalized


def project_skills_dir(project_dir: Path) -> Path:
    return project_dir / SKILLS_DIRNAME


def global_skills_dir(app_dir: Path) -> Path:
    return app_dir / SKILLS_DIRNAME


def skill_bundle_root_relative(session_id: str) -> str:
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        raise ValueError("session_id is required for skill bundle paths.")
    return str(Path(SKILL_BUNDLE_DIRNAME) / normalized_session_id)


def skill_manifest_relative(session_id: str) -> str:
    return str(Path(skill_bundle_root_relative(session_id)) / SKILL_MANIFEST_FILENAME)


def is_skill_bundle_relative_path(relative_path: str, session_id: str) -> bool:
    normalized_path = str(relative_path or "").strip()
    if not normalized_path:
        return False
    prefix = Path(skill_bundle_root_relative(session_id))
    path_obj = Path(normalized_path)
    return path_obj == prefix or prefix in path_obj.parents


def discover_local_skills(
    *,
    app_dir: Path,
    project_dir: Path | None = None,
) -> dict[str, SkillDescriptor]:
    discovered = _discover_local_skill_source(global_skills_dir(app_dir), source_type="global")
    if project_dir is not None:
        discovered.update(
            _discover_local_skill_source(
                project_skills_dir(project_dir),
                source_type="project",
            )
        )
    return discovered


def copy_local_skill_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        copy_function=shutil.copy2,
        ignore=_ignore_symlink_names,
    )


def build_manifest_payload(
    *,
    session_id: str,
    bundle_root: str,
    entries: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "session_id": str(session_id or "").strip(),
        "skills_root": str(bundle_root or "").strip(),
        "enabled_skill_ids": [str(item.get("id", "")).strip() for item in entries if str(item.get("id", "")).strip()],
        "entries": entries,
        "warnings": warnings,
    }


def render_skills_prompt(
    *,
    locale: str,
    bundle_root: str,
    manifest_path: str,
    skills_state: dict[str, Any] | None,
) -> str:
    normalized_state = skills_state if isinstance(skills_state, dict) else {}
    entries = normalized_state.get("entries")
    warnings = normalized_state.get("warnings")
    if not isinstance(entries, list) or not entries:
        return ""
    lines: list[str] = []
    if locale == "zh":
        lines.extend(
            [
                "",
                "已启用 Skills:",
                f"- skills 根目录: {bundle_root}",
                f"- manifest 路径: {manifest_path}",
                "- 这些 skills 已复制到项目目录中，默认按只读资源处理。",
                "- 需要查看文档、脚本或二进制资源时，使用 shell 按路径访问。",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Enabled Skills:",
                f"- skills root: {bundle_root}",
                f"- manifest path: {manifest_path}",
                "- These skills have been copied into the project directory and should be treated as read-only runtime assets.",
                "- Use shell with explicit paths when you need to inspect docs, scripts, or binary resources.",
            ]
        )
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue
        skill_id = str(raw_entry.get("id", "")).strip()
        if not skill_id:
            continue
        display_name = str(
            raw_entry.get("name_cn" if locale == "zh" else "name", "")
            or raw_entry.get("name", "")
            or skill_id
        ).strip()
        description = str(
            raw_entry.get("description_cn" if locale == "zh" else "description", "")
            or raw_entry.get("description", "")
        ).strip()
        doc_path = str(raw_entry.get("main_doc_project_path", "")).strip()
        resource_hint = int(raw_entry.get("resource_count", 0) or 0)
        lines.append(
            f"- {skill_id}: {display_name}"
            + (f" | {description}" if description else "")
            + (f" | doc={doc_path}" if doc_path else "")
            + (f" | resources={resource_hint}" if resource_hint > 0 else "")
        )
    if isinstance(warnings, list):
        for raw_warning in warnings:
            if not isinstance(raw_warning, dict):
                continue
            message = str(
                raw_warning.get("message_cn" if locale == "zh" else "message", "")
                or raw_warning.get("message", "")
            ).strip()
            if message:
                lines.append(f"- {'告警' if locale == 'zh' else 'warning'}: {message}")
    return "\n".join(lines).strip()


def skills_catalog_for_agent(
    *,
    session_id: str,
    locale: str,
    skills_state: dict[str, Any] | None,
) -> dict[str, Any]:
    bundle_root = skill_bundle_root_relative(session_id)
    manifest_path = skill_manifest_relative(session_id)
    normalized_state = skills_state if isinstance(skills_state, dict) else {}
    entries: list[dict[str, Any]] = []
    for raw_entry in normalized_state.get("entries", []):
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        localized_doc = str(
            entry.get("localized_doc_project_path" if locale == "zh" else "main_doc_project_path", "")
            or entry.get("main_doc_project_path", "")
        ).strip()
        entry["preferred_doc_project_path"] = localized_doc
        entries.append(entry)
    return {
        "bundle_root": bundle_root,
        "manifest_path": manifest_path,
        "entries": entries,
        "warnings": list(normalized_state.get("warnings", []))
        if isinstance(normalized_state.get("warnings"), list)
        else [],
    }


def describe_skill_drift(
    previous_entry: dict[str, Any] | None,
    current_entry: dict[str, Any],
) -> bool:
    if not isinstance(previous_entry, dict):
        return False
    previous_files = _file_signature_map(previous_entry.get("files"))
    current_files = _file_signature_map(current_entry.get("files"))
    if previous_files != current_files:
        return True
    return (
        str(previous_entry.get("source_type", "")).strip()
        != str(current_entry.get("source_type", "")).strip()
        or str(previous_entry.get("source_path", "")).strip()
        != str(current_entry.get("source_path", "")).strip()
    )


def _discover_local_skill_source(
    source_root: Path,
    *,
    source_type: str,
) -> dict[str, SkillDescriptor]:
    discovered: dict[str, SkillDescriptor] = {}
    if not source_root.exists() or not source_root.is_dir():
        return discovered
    for candidate in sorted(source_root.iterdir(), key=lambda item: item.name.lower()):
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        try:
            descriptor = load_local_skill_descriptor(
                source_root=source_root,
                skill_dir=candidate,
                source_type=source_type,
            )
        except ValueError:
            continue
        discovered[descriptor.id] = descriptor
    return discovered


def load_local_skill_descriptor(
    *,
    source_root: Path,
    skill_dir: Path,
    source_type: str,
) -> SkillDescriptor:
    metadata_path = skill_dir / SKILL_METADATA_FILENAME
    if not metadata_path.exists() or not metadata_path.is_file():
        raise ValueError(f"Skill metadata is missing: {metadata_path}")
    payload = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
    metadata = payload.get("skill") if isinstance(payload.get("skill"), dict) else payload
    if not isinstance(metadata, dict):
        raise ValueError(f"Skill metadata must decode to an object: {metadata_path}")
    directory_id = normalize_skill_id(skill_dir.name)
    metadata_id = str(metadata.get("id", directory_id) or directory_id).strip()
    skill_id = normalize_skill_id(metadata_id)
    if skill_id != directory_id:
        raise ValueError(
            f"Skill metadata id '{skill_id}' does not match directory '{directory_id}'."
        )
    main_doc_path = skill_dir / SKILL_MAIN_DOC_FILENAME
    if not main_doc_path.exists() or not main_doc_path.is_file():
        raise ValueError(f"Skill main doc is missing: {main_doc_path}")
    localized_doc_path = skill_dir / SKILL_MAIN_DOC_CN_FILENAME
    files = tuple(_collect_local_skill_files(skill_dir))
    return SkillDescriptor(
        id=skill_id,
        name=str(metadata.get("name", skill_id) or skill_id).strip() or skill_id,
        name_cn=str(metadata.get("name_cn", metadata.get("name", skill_id)) or skill_id).strip()
        or skill_id,
        description=str(metadata.get("description", "") or "").strip(),
        description_cn=str(
            metadata.get("description_cn", metadata.get("description", "")) or ""
        ).strip(),
        tags=tuple(
            str(item).strip()
            for item in metadata.get("tags", [])
            if str(item).strip()
        ),
        source_type=str(source_type or "").strip() or "global",
        source_root=str(source_root.resolve()),
        source_path=str(skill_dir.resolve()),
        main_doc_path=SKILL_MAIN_DOC_FILENAME,
        localized_doc_path=(
            SKILL_MAIN_DOC_CN_FILENAME if localized_doc_path.exists() else SKILL_MAIN_DOC_FILENAME
        ),
        files=files,
    )


def _collect_local_skill_files(skill_dir: Path) -> list[SkillFileRecord]:
    records: list[SkillFileRecord] = []
    for file_path in sorted(skill_dir.rglob("*"), key=lambda item: str(item.relative_to(skill_dir))):
        if file_path.is_symlink() or not file_path.is_file():
            continue
        relative_path = str(file_path.relative_to(skill_dir))
        content = file_path.read_bytes()
        mode = file_path.stat().st_mode & 0o777
        records.append(
            SkillFileRecord(
                relative_path=relative_path,
                sha256=hash_bytes(content),
                size=len(content),
                is_binary=_is_binary_bytes(content),
                mode=format(mode, "03o"),
                is_executable=bool(mode & 0o111),
            )
        )
    return records


def _is_binary_bytes(content: bytes) -> bool:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _ignore_symlink_names(current_dir: str, names: list[str]) -> set[str]:
    current = Path(current_dir)
    ignored: set[str] = set()
    for name in names:
        if (current / name).is_symlink():
            ignored.add(name)
    return ignored


def _file_signature_map(files: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(files, list):
        return {}
    mapped: dict[str, dict[str, Any]] = {}
    for raw_file in files:
        if not isinstance(raw_file, dict):
            continue
        relative_path = str(raw_file.get("relative_path", "")).strip()
        if not relative_path:
            continue
        mapped[relative_path] = {
            "sha256": str(raw_file.get("sha256", "")).strip(),
            "size": int(raw_file.get("size", 0) or 0),
            "mode": str(raw_file.get("mode", "")).strip(),
            "is_executable": bool(raw_file.get("is_executable", False)),
        }
    return mapped
