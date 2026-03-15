from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from opencompany.utils import utc_now


STREAM_EVENT_TYPES = {"llm_token", "llm_reasoning", "shell_stream"}


@dataclass(slots=True)
class EventBatch:
    timestamp: str
    events: list[dict[str, Any]]

    def as_record(self) -> dict[str, Any]:
        return {
            "event_type": "event_batch",
            "timestamp": self.timestamp,
            "payload": {"events": self.events},
        }


class EventHub:
    """Best-effort fan-out delivery for runtime events to websocket subscribers."""

    def __init__(self, *, queue_size: int = 2000) -> None:
        self._queue_size = max(1, int(queue_size))
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, record: dict[str, Any]) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(record)
            except asyncio.QueueFull:
                # If the queue is still full after dropping one event, skip this subscriber.
                continue


def collapse_stream_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse adjacent token/reasoning/shell-stream events with matching envelopes."""
    collapsed: list[dict[str, Any]] = []
    for record in events:
        if not collapsed:
            collapsed.append(record)
            continue
        last = collapsed[-1]
        if not _can_merge_stream_event(last, record):
            collapsed.append(record)
            continue
        merged = _merge_stream_event(last, record)
        collapsed[-1] = merged
    return collapsed


def build_event_batch(events: list[dict[str, Any]]) -> EventBatch:
    return EventBatch(timestamp=utc_now(), events=collapse_stream_events(events))


def _can_merge_stream_event(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_type = str(previous.get("event_type", ""))
    current_type = str(current.get("event_type", ""))
    if previous_type != current_type or previous_type not in STREAM_EVENT_TYPES:
        return False
    for key in ("session_id", "agent_id", "parent_agent_id", "phase", "workspace_id"):
        if previous.get(key) != current.get(key):
            return False
    previous_payload = previous.get("payload")
    current_payload = current.get("payload")
    if not isinstance(previous_payload, dict) or not isinstance(current_payload, dict):
        return False
    return _stream_payload_field(previous_type) in previous_payload and _stream_payload_field(
        current_type
    ) in current_payload


def _merge_stream_event(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    event_type = str(previous.get("event_type", ""))
    payload_key = _stream_payload_field(event_type)
    previous_payload = dict(previous.get("payload", {}))
    current_payload = current.get("payload", {})
    previous_value = str(previous_payload.get(payload_key, ""))
    current_value = ""
    if isinstance(current_payload, dict):
        current_value = str(current_payload.get(payload_key, ""))
    previous_payload[payload_key] = previous_value + current_value
    return {
        **previous,
        "timestamp": current.get("timestamp", previous.get("timestamp")),
        "payload": previous_payload,
    }


def _stream_payload_field(event_type: str) -> str:
    if event_type == "shell_stream":
        return "text"
    return "token"
