from __future__ import annotations

import asyncio
import base64
import json
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opencompany.config import OpenCompanyConfig
from opencompany.models import AgentNode, EventRecord
from opencompany.storage import Storage
from opencompany.utils import ensure_directory, json_ready, utc_now


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(json_ready(record), ensure_ascii=False) + "\n")


def diagnostics_path_for_app(app_dir: Path) -> Path:
    config = OpenCompanyConfig.load(app_dir)
    data_dir = ensure_directory(app_dir / config.project.data_dir)
    return data_dir / config.logging.diagnostics_filename


class DiagnosticLogger:
    def __init__(self, jsonl_path: Path) -> None:
        self.jsonl_path = jsonl_path
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        *,
        component: str,
        event_type: str,
        level: str = "info",
        session_id: str | None = None,
        agent_id: str | None = None,
        message: str = "",
        payload: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        record_payload = dict(payload or {})
        if error is not None:
            record_payload.setdefault("error", str(error))
            record_payload.setdefault("error_type", error.__class__.__name__)
            if error.__traceback__ is not None:
                record_payload.setdefault(
                    "traceback",
                    "".join(traceback.format_exception(type(error), error, error.__traceback__)),
                )
        record = {
            "timestamp": utc_now(),
            "component": component,
            "event_type": event_type,
            "level": level,
            "session_id": session_id,
            "agent_id": agent_id,
            "message": message,
            "payload": json_ready(record_payload),
        }
        append_jsonl(self.jsonl_path, record)
        return record

    def read(self, *, session_id: str | None = None) -> list[dict[str, Any]]:
        if not self.jsonl_path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.jsonl_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if session_id is not None and record.get("session_id") != session_id:
                    continue
                records.append(record)
        return records


class StructuredLogger:
    def __init__(
        self,
        storage: Storage,
        jsonl_path: Path,
        diagnostic_logger: DiagnosticLogger | None = None,
    ) -> None:
        self.storage = storage
        self.jsonl_path = jsonl_path
        self.diagnostic_logger = diagnostic_logger
        self.subscribers: list[Callable[[dict[str, Any]], None]] = []
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self.subscribers.append(callback)

    def log(
        self,
        *,
        session_id: str,
        agent_id: str | None,
        parent_agent_id: str | None,
        event_type: str,
        phase: str,
        payload: dict[str, Any],
        workspace_id: str | None,
        checkpoint_seq: int = 0,
    ) -> None:
        record = EventRecord(
            timestamp=utc_now(),
            session_id=session_id,
            agent_id=agent_id,
            parent_agent_id=parent_agent_id,
            event_type=event_type,
            phase=phase,
            payload=payload,
            workspace_id=workspace_id,
            checkpoint_seq=checkpoint_seq,
        )
        ready = json_ready(record)
        self.storage.append_event(record)
        append_jsonl(self.jsonl_path, ready)
        for subscriber in self.subscribers:
            try:
                subscriber(ready)
            except asyncio.CancelledError:
                # UI subscriber delivery can race with shutdown; never abort runtime logging.
                if self.diagnostic_logger is not None:
                    self.diagnostic_logger.log(
                        component="runtime_logger",
                        event_type="subscriber_delivery_cancelled",
                        level="warning",
                        session_id=session_id,
                        agent_id=agent_id,
                        payload={
                            "event_type": event_type,
                            "phase": phase,
                            "subscriber": repr(subscriber),
                        },
                    )
                continue
            except Exception as exc:
                # Subscriber delivery is best-effort and must not break runtime logging.
                if self.diagnostic_logger is not None:
                    self.diagnostic_logger.log(
                        component="runtime_logger",
                        event_type="subscriber_delivery_failed",
                        level="warning",
                        session_id=session_id,
                        agent_id=agent_id,
                        payload={
                            "event_type": event_type,
                            "phase": phase,
                            "subscriber": repr(subscriber),
                        },
                        error=exc,
                    )
                continue


class AgentMessageLogger:
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = ensure_directory(session_dir)
        self._message_counts: dict[str, int] = {}

    def messages_path(self, agent_id: str) -> Path:
        return self.session_dir / f"{agent_id}_messages.jsonl"

    def append(
        self,
        agent: AgentNode,
        message: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message_index = self._message_count(agent.id)
        record = {
            "timestamp": utc_now(),
            "session_id": agent.session_id,
            "agent_id": agent.id,
            "agent_name": agent.name,
            "agent_role": agent.role.value,
            "message_index": message_index,
            "step_count": int(agent.step_count),
            "role": message.get("role"),
            "message": message,
        }
        if metadata:
            record.update(json_ready(metadata))
        append_jsonl(self.messages_path(agent.id), record)
        self._message_counts[agent.id] = message_index + 1
        return record

    def sync_conversation(self, agent: AgentNode) -> None:
        start_index = self._message_count(agent.id)
        for message in agent.conversation[start_index:]:
            self.append(agent, message)

    def read(self, agent_id: str) -> list[dict[str, Any]]:
        path = self.messages_path(agent_id)
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
        return records

    def read_all(self) -> dict[str, list[dict[str, Any]]]:
        records: dict[str, list[dict[str, Any]]] = {}
        for path in sorted(self.session_dir.glob("*_messages.jsonl")):
            agent_id = path.name.removesuffix("_messages.jsonl")
            records[agent_id] = self.read(agent_id)
        return records

    def list_records(
        self,
        *,
        agent_id: str | None = None,
        cursor: str | None = None,
        limit: int = 500,
        tail: int | None = None,
    ) -> dict[str, Any]:
        try:
            normalized_limit = max(1, min(5000, int(limit)))
        except (TypeError, ValueError):
            normalized_limit = 500
        normalized_agent_id = str(agent_id or "").strip() or None
        normalized_tail: int | None = None
        if tail is not None:
            try:
                normalized_tail = max(1, min(5000, int(tail)))
            except (TypeError, ValueError):
                normalized_tail = 500

        offsets = self._decode_cursor_offsets(cursor)
        candidate_agent_ids = (
            [normalized_agent_id] if normalized_agent_id is not None else self._agent_ids()
        )
        records: list[dict[str, Any]] = []
        for current_agent_id in candidate_agent_ids:
            last_index = offsets.get(current_agent_id, -1)
            for record in self.read(current_agent_id):
                message_index = self._message_index(record)
                if message_index <= last_index:
                    continue
                records.append(record)

        records.sort(key=self._message_sort_key)
        if normalized_tail is not None and cursor is None:
            records = records[-normalized_tail:]
            has_more = False
        else:
            has_more = len(records) > normalized_limit
            if has_more:
                records = records[:normalized_limit]

        next_offsets = dict(offsets)
        for record in records:
            current_agent_id = str(record.get("agent_id", "")).strip()
            if not current_agent_id:
                continue
            message_index = self._message_index(record)
            if message_index > next_offsets.get(current_agent_id, -1):
                next_offsets[current_agent_id] = message_index
        next_cursor = self._encode_cursor_offsets(next_offsets) if next_offsets else None
        return {
            "messages": records,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    def _message_count(self, agent_id: str) -> int:
        count = self._message_counts.get(agent_id)
        if count is not None:
            return count
        path = self.messages_path(agent_id)
        if not path.exists():
            self._message_counts[agent_id] = 0
            return 0
        with path.open("r", encoding="utf-8") as handle:
            count = sum(1 for line in handle if line.strip())
        self._message_counts[agent_id] = count
        return count

    def _agent_ids(self) -> list[str]:
        return [
            path.name.removesuffix("_messages.jsonl")
            for path in sorted(self.session_dir.glob("*_messages.jsonl"))
        ]

    @staticmethod
    def _message_index(record: dict[str, Any]) -> int:
        try:
            return int(record.get("message_index", -1))
        except (TypeError, ValueError):
            return -1

    @classmethod
    def _message_sort_key(cls, record: dict[str, Any]) -> tuple[str, str, int]:
        return (
            str(record.get("timestamp", "")),
            str(record.get("agent_id", "")),
            cls._message_index(record),
        )

    @staticmethod
    def _decode_cursor_offsets(cursor: str | None) -> dict[str, int]:
        if cursor is None:
            return {}
        text = str(cursor).strip()
        if not text:
            return {}
        try:
            payload = base64.urlsafe_b64decode(text.encode("ascii"))
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        raw_offsets = data.get("offsets")
        if not isinstance(raw_offsets, dict):
            return {}
        offsets: dict[str, int] = {}
        for key, value in raw_offsets.items():
            agent_id = str(key).strip()
            if not agent_id:
                continue
            try:
                offsets[agent_id] = int(value)
            except (TypeError, ValueError):
                continue
        return offsets

    @staticmethod
    def _encode_cursor_offsets(offsets: dict[str, int]) -> str | None:
        if not offsets:
            return None
        payload = {
            "version": 1,
            "offsets": {key: int(value) for key, value in sorted(offsets.items())},
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(encoded).decode("ascii")
