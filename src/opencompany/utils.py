from __future__ import annotations

import json
import locale
import os
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def stable_json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return {k: json_ready(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def detect_system_locale() -> str:
    language, _ = locale.getlocale()
    language = language or locale.getdefaultlocale()[0] or ""
    normalized = language.lower()
    if normalized.startswith("zh"):
        return "zh"
    if normalized.startswith("en"):
        return "en"
    return "en"


def safe_relative_path(root: Path, target: Path) -> str:
    return str(target.resolve().relative_to(root.resolve()))


def resolve_in_workspace(workspace_root: Path, relative_path: str) -> Path:
    candidate = (workspace_root / relative_path).resolve()
    workspace_root = workspace_root.resolve()
    if candidate != workspace_root and workspace_root not in candidate.parents:
        raise ValueError(f"Path escapes workspace: {relative_path}")
    return candidate


def truncate_text(text: str, max_chars: int = 8000) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 19] + "\n...[truncated]..."


def hash_bytes(content: bytes) -> str:
    return sha256(content).hexdigest()


def load_project_env(app_dir: Path) -> dict[str, str]:
    env_path = app_dir / ".env"
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded
