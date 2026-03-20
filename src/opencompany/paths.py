from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from opencompany.config import OpenCompanyConfig
from opencompany.utils import ensure_directory

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@dataclass(slots=True)
class RuntimePaths:
    app_dir: Path
    data_dir: Path
    sessions_dir: Path
    prompts_dir: Path
    docs_dir: Path

    @classmethod
    def create(cls, app_dir: Path, config: OpenCompanyConfig) -> "RuntimePaths":
        app_dir = app_dir.resolve()
        data_dir = ensure_directory(app_dir / config.project.data_dir)
        sessions_dir = ensure_directory(data_dir / "sessions")
        prompts_dir = app_dir / "prompts"
        docs_dir = app_dir / "docs"
        return cls(
            app_dir=app_dir,
            data_dir=data_dir,
            sessions_dir=sessions_dir,
            prompts_dir=prompts_dir,
            docs_dir=docs_dir,
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "opencompany.db"

    @staticmethod
    def normalize_session_id(session_id: str) -> str:
        normalized = str(session_id or "").strip()
        if not normalized:
            raise ValueError("Session ID is required.")
        if not SESSION_ID_RE.fullmatch(normalized):
            raise ValueError(
                "Invalid session ID. Use letters/numbers and optional '-' or '_' only."
            )
        return normalized

    def session_dir(self, session_id: str, *, create: bool = True) -> Path:
        normalized = self.normalize_session_id(session_id)
        sessions_root = self.sessions_dir.resolve()
        session_path = (sessions_root / normalized).resolve()
        if session_path.parent != sessions_root:
            raise ValueError("Session path escapes sessions directory.")
        if create:
            ensure_directory(session_path)
        return session_path

    def existing_session_dir(self, session_id: str) -> Path:
        normalized = self.normalize_session_id(session_id)
        session_path = self.session_dir(normalized, create=False)
        if not session_path.exists() or not session_path.is_dir():
            raise ValueError(f"Session folder does not exist: {normalized}")
        return session_path

    def session_logs_path(self, session_id: str, filename: str) -> Path:
        return self.session_dir(session_id, create=True) / filename

    def session_agent_messages_path(self, session_id: str, agent_id: str) -> Path:
        return self.session_dir(session_id, create=True) / f"{agent_id}_messages.jsonl"

    def diagnostics_path(self, filename: str) -> Path:
        return self.data_dir / filename

    @property
    def mcp_oauth_tokens_path(self) -> Path:
        return self.data_dir / "mcp_oauth_tokens.json"
