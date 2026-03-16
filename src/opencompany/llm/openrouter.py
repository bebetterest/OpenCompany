from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

from opencompany.logging import append_jsonl
from opencompany.models import ToolCall
from opencompany.utils import ensure_directory, utc_now

TokenCallback = Callable[[str], Awaitable[None] | None]
RetryCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
OPENROUTER_HEADER_APP_NAME = "OpenCompany"
OPENROUTER_HEADER_SITE_URL = "https://github.com/bebetterest/OpenCompany"


class SseParser:
    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[str]:
        self._buffer += chunk
        events: list[str] = []
        while "\n\n" in self._buffer:
            raw_event, self._buffer = self._buffer.split("\n\n", 1)
            lines = []
            for line in raw_event.splitlines():
                if line.startswith("data:"):
                    lines.append(line[5:].strip())
            if lines:
                events.append("\n".join(lines))
        return events


@dataclass(slots=True)
class ChatResult:
    content: str
    raw_events: list[dict[str, Any]]
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str = ""
    reasoning_details: list[dict[str, Any]] = field(default_factory=list)
    response_id: str | None = None
    created: int | None = None
    model: str | None = None
    object: str | None = None
    system_fingerprint: str | None = None
    provider: str | None = None
    usage: dict[str, Any] | None = None
    choice_index: int = 0
    finish_reason: str | None = None
    native_finish_reason: str | None = None
    response_error: dict[str, Any] | None = None

    def response_payload(self, message: dict[str, Any]) -> dict[str, Any]:
        choice = {
            "index": self.choice_index,
            "message": message,
            "finish_reason": self.finish_reason,
            "native_finish_reason": self.native_finish_reason,
        }
        if self.response_error is not None:
            choice["error"] = self.response_error
        payload = {
            "id": self.response_id,
            "created": self.created,
            "model": self.model,
            "object": _final_response_object(self.object),
            "system_fingerprint": self.system_fingerprint,
            "provider": self.provider,
            "usage": self.usage,
            "choices": [choice],
        }
        return {
            key: value
            for key, value in payload.items()
            if value is not None or key == "choices"
        }


class OpenRouterClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: int = 120,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        request_response_log_path: Path | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.request_response_log_path = request_response_log_path
        self.request_response_log_dir: Path | None = None
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": OPENROUTER_HEADER_SITE_URL,
            "X-Title": OPENROUTER_HEADER_APP_NAME,
        }

    async def stream_chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        timeout_seconds: float | None = None,
        debug_agent_id: str | None = None,
        debug_module: str | None = None,
        on_token: TokenCallback | None = None,
        on_reasoning: TokenCallback | None = None,
        on_retry: RetryCallback | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> ChatResult:
        import httpx

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = parallel_tool_calls
        effective_timeout = float(self.timeout_seconds)
        if timeout_seconds is not None:
            try:
                parsed_timeout = float(timeout_seconds)
            except (TypeError, ValueError):
                parsed_timeout = 0.0
            if parsed_timeout > 0:
                effective_timeout = parsed_timeout
        max_attempts = self.max_retries + 1
        attempt = 0
        while True:
            parser = SseParser()
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            raw_events: list[dict[str, Any]] = []
            tool_call_parts: dict[int, dict[str, str]] = {}
            reasoning_detail_parts: dict[int, dict[str, Any]] = {}
            response_id: str | None = None
            created: int | None = None
            response_model: str | None = None
            response_object: str | None = None
            system_fingerprint: str | None = None
            provider: str | None = None
            usage: dict[str, Any] | None = None
            choice_index = 0
            finish_reason: str | None = None
            native_finish_reason: str | None = None
            response_error: dict[str, Any] | None = None
            received_event = False
            status_code: int | None = None
            status_text: str | None = None

            try:
                async with httpx.AsyncClient(timeout=effective_timeout) as client:
                    async with client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        headers=self.headers,
                        json=payload,
                    ) as response:
                        status_code = getattr(response, "status_code", None)
                        status_text = self._response_status_text(response)
                        response.raise_for_status()
                        async for chunk in response.aiter_text():
                            for event_data in parser.feed(chunk):
                                if event_data == "[DONE]":
                                    continue
                                received_event = True
                                event = json.loads(event_data)
                                raw_events.append(event)
                                if response_id is None and isinstance(event.get("id"), str):
                                    response_id = event["id"]
                                if created is None and isinstance(event.get("created"), int):
                                    created = event["created"]
                                if response_model is None and isinstance(event.get("model"), str):
                                    response_model = event["model"]
                                if response_object is None and isinstance(event.get("object"), str):
                                    response_object = event["object"]
                                if system_fingerprint is None and isinstance(event.get("system_fingerprint"), str):
                                    system_fingerprint = event["system_fingerprint"]
                                if provider is None and isinstance(event.get("provider"), str):
                                    provider = event["provider"]
                                if isinstance(event.get("usage"), dict):
                                    usage = event["usage"]
                                choices = event.get("choices", [])
                                if not choices:
                                    continue
                                choice = choices[0]
                                if isinstance(choice.get("index"), int):
                                    choice_index = int(choice["index"])
                                if isinstance(choice.get("finish_reason"), str):
                                    finish_reason = choice["finish_reason"]
                                if isinstance(choice.get("native_finish_reason"), str):
                                    native_finish_reason = choice["native_finish_reason"]
                                if isinstance(choice.get("error"), dict):
                                    response_error = choice["error"]
                                delta = choice.get("delta", {})
                                for reasoning_text in _reasoning_text_fragments(delta):
                                    reasoning_parts.append(reasoning_text)
                                    if on_reasoning:
                                        maybe = on_reasoning(reasoning_text)
                                        if hasattr(maybe, "__await__"):
                                            await maybe
                                _merge_reasoning_details(
                                    reasoning_detail_parts,
                                    delta.get("reasoning_details", []),
                                )
                                text = delta.get("content")
                                if text:
                                    content_parts.append(text)
                                    if on_token:
                                        maybe = on_token(text)
                                        if hasattr(maybe, "__await__"):
                                            await maybe
                                for tool_delta in delta.get("tool_calls", []):
                                    index = int(tool_delta.get("index", 0))
                                    current = tool_call_parts.setdefault(
                                        index,
                                        {"id": "", "name": "", "arguments": ""},
                                    )
                                    if tool_delta.get("id"):
                                        current["id"] = str(tool_delta["id"])
                                    function = tool_delta.get("function", {})
                                    if function.get("name"):
                                        current["name"] += str(function["name"])
                                    if function.get("arguments"):
                                        current["arguments"] += str(function["arguments"])
            except Exception as exc:
                response = exc.response if isinstance(exc, httpx.HTTPStatusError) else None
                if response is not None:
                    status_code = response.status_code
                    status_text = self._response_status_text(response)
                partial_tool_calls = [
                    {
                        "id": chunk["id"] or f"tool-call-{index}",
                        "name": chunk["name"],
                        "arguments_json": chunk["arguments"] or "{}",
                    }
                    for index, chunk in sorted(tool_call_parts.items())
                ]
                partial_reasoning_details = [
                    reasoning_detail_parts[index]
                    for index in sorted(reasoning_detail_parts)
                ]
                partial_content = "".join(content_parts)
                partial_reasoning = "".join(reasoning_parts)
                partial_response_payload: dict[str, Any] | None = None
                if (
                    received_event
                    or partial_content.strip()
                    or partial_reasoning.strip()
                    or partial_tool_calls
                ):
                    partial_response_payload = {
                        "id": response_id,
                        "created": created,
                        "model": response_model,
                        "object": _final_response_object(response_object),
                        "system_fingerprint": system_fingerprint,
                        "provider": provider,
                        "usage": usage,
                        "choice_index": choice_index,
                        "finish_reason": finish_reason,
                        "native_finish_reason": native_finish_reason,
                        "error": response_error,
                        "content": partial_content,
                        "reasoning": partial_reasoning,
                        "reasoning_details": partial_reasoning_details,
                        "tool_calls": partial_tool_calls,
                        "stream": {
                            "raw_event_count": len(raw_events),
                            "raw_events": raw_events,
                            "incomplete": True,
                        },
                    }
                self._append_debug_record(
                    attempt=attempt + 1,
                    request_payload=payload,
                    response_payload=partial_response_payload,
                    status_code=status_code,
                    status_text=status_text,
                    error=exc,
                    debug_agent_id=debug_agent_id,
                    debug_module=debug_module,
                )
                if not self._should_retry_stream_error(
                    exc=exc,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    has_partial_output=received_event,
                ):
                    raise
                retry_delay_seconds = self._retry_delay_seconds(attempt=attempt, response=response)
                if on_retry is not None:
                    retry_payload = {
                        "attempt": attempt + 1,
                        "max_attempts": max_attempts,
                        "max_retries": self.max_retries,
                        "next_attempt": attempt + 2,
                        "retry_delay_seconds": retry_delay_seconds,
                        "retry_reason": self._retry_reason(exc),
                        "status_code": status_code,
                        "status_text": status_text,
                        "error_type": exc.__class__.__name__,
                        "error": str(exc),
                    }
                    try:
                        maybe = on_retry(retry_payload)
                        if hasattr(maybe, "__await__"):
                            await maybe
                    except Exception:
                        pass
                await asyncio.sleep(retry_delay_seconds)
                attempt += 1
                continue

            tool_calls = [
                ToolCall(
                    id=chunk["id"] or f"tool-call-{index}",
                    name=chunk["name"],
                    arguments_json=chunk["arguments"] or "{}",
                )
                for index, chunk in sorted(tool_call_parts.items())
            ]
            reasoning_details = [
                reasoning_detail_parts[index]
                for index in sorted(reasoning_detail_parts)
            ]
            content = "".join(content_parts)
            reasoning = "".join(reasoning_parts)
            if _should_retry_empty_stream_response(
                content=content,
                reasoning=reasoning,
                tool_calls=tool_calls,
                response_error=response_error,
                finish_reason=finish_reason,
                native_finish_reason=native_finish_reason,
                attempt=attempt,
                max_attempts=max_attempts,
            ):
                self._append_debug_record(
                    attempt=attempt + 1,
                    request_payload=payload,
                    response_payload={
                        "id": response_id,
                        "created": created,
                        "model": response_model,
                        "object": _final_response_object(response_object),
                        "system_fingerprint": system_fingerprint,
                        "provider": provider,
                        "usage": usage,
                        "choice_index": choice_index,
                        "finish_reason": finish_reason,
                        "native_finish_reason": native_finish_reason,
                        "error": response_error,
                        "content": content,
                        "reasoning": reasoning,
                        "reasoning_details": reasoning_details,
                        "tool_calls": [
                            {
                                "id": tool_call.id,
                                "name": tool_call.name,
                                "arguments_json": tool_call.arguments_json,
                            }
                            for tool_call in tool_calls
                        ],
                        "stream": {
                            "raw_event_count": len(raw_events),
                            "raw_events": raw_events,
                            "retry_reason": "empty_stream_response",
                        },
                    },
                    status_code=status_code,
                    status_text=status_text,
                    error=None,
                    debug_agent_id=debug_agent_id,
                    debug_module=debug_module,
                )
                await asyncio.sleep(self._retry_delay_seconds(attempt=attempt))
                attempt += 1
                continue
            self._append_debug_record(
                attempt=attempt + 1,
                request_payload=payload,
                response_payload={
                    "id": response_id,
                    "created": created,
                    "model": response_model,
                    "object": _final_response_object(response_object),
                    "system_fingerprint": system_fingerprint,
                    "provider": provider,
                    "usage": usage,
                    "choice_index": choice_index,
                    "finish_reason": finish_reason,
                    "native_finish_reason": native_finish_reason,
                    "error": response_error,
                    "content": content,
                    "reasoning": reasoning,
                    "reasoning_details": reasoning_details,
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "arguments_json": tool_call.arguments_json,
                        }
                        for tool_call in tool_calls
                    ],
                    "stream": {
                        "raw_event_count": len(raw_events),
                        "raw_events": raw_events,
                    },
                },
                status_code=status_code,
                status_text=status_text,
                error=None,
                debug_agent_id=debug_agent_id,
                debug_module=debug_module,
            )
            return ChatResult(
                content=content,
                raw_events=raw_events,
                tool_calls=tool_calls,
                reasoning=reasoning,
                reasoning_details=reasoning_details,
                response_id=response_id,
                created=created,
                model=response_model,
                object=response_object,
                system_fingerprint=system_fingerprint,
                provider=provider,
                usage=usage,
                choice_index=choice_index,
                finish_reason=finish_reason,
                native_finish_reason=native_finish_reason,
                response_error=response_error,
            )

    def _append_debug_record(
        self,
        *,
        attempt: int,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any] | None,
        status_code: int | None,
        status_text: str | None,
        error: Exception | None,
        debug_agent_id: str | None = None,
        debug_module: str | None = None,
    ) -> None:
        log_path = self._debug_log_path(
            debug_agent_id=debug_agent_id,
            debug_module=debug_module,
        )
        if log_path is None:
            return
        record: dict[str, Any] = {
            "timestamp": utc_now(),
            "attempt": attempt,
            "request": {
                "method": "POST",
                "url": f"{self.base_url}/chat/completions",
                "payload": request_payload,
            },
            "response": response_payload,
            "status_code": status_code,
            "status_text": status_text,
        }
        if debug_agent_id or debug_module:
            record["scope"] = {
                "agent_id": str(debug_agent_id or ""),
                "module": str(debug_module or ""),
            }
        if error is not None:
            record["error"] = {
                "type": error.__class__.__name__,
                "message": str(error),
            }
        append_jsonl(log_path, record)

    def _debug_log_path(
        self,
        *,
        debug_agent_id: str | None,
        debug_module: str | None,
    ) -> Path | None:
        if self.request_response_log_dir is not None:
            debug_dir = ensure_directory(Path(self.request_response_log_dir))
            agent_part = self._normalize_scope_part(debug_agent_id, fallback="agent_unknown")
            module_part = self._normalize_scope_part(debug_module, fallback="module_unknown")
            return debug_dir / f"{agent_part}__{module_part}.jsonl"
        return self.request_response_log_path

    @staticmethod
    def _normalize_scope_part(value: str | None, *, fallback: str) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return fallback
        normalized = re.sub(r"[^a-z0-9._-]+", "_", raw).strip("._-")
        return normalized or fallback

    @staticmethod
    def _is_retryable_status_code(status_code: int) -> bool:
        return 400 <= status_code < 600

    def _should_retry_stream_error(
        self,
        *,
        exc: Exception,
        attempt: int,
        max_attempts: int,
        has_partial_output: bool,
    ) -> bool:
        if has_partial_output or attempt >= max_attempts - 1:
            return False

        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            response = exc.response
            if response is None:
                return False
            return self._is_retryable_status_code(response.status_code)

        retryable_transport_errors = (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        )
        return isinstance(exc, retryable_transport_errors)

    @classmethod
    def _response_status_text(cls, response: Any | None) -> str | None:
        if response is None:
            return None
        reason_phrase = getattr(response, "reason_phrase", None)
        if isinstance(reason_phrase, str):
            normalized = reason_phrase.strip()
            if normalized and normalized.lower() != "<none>":
                return normalized
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return cls._status_text_from_code(status_code)
        return None

    @staticmethod
    def _status_text_from_code(status_code: int) -> str | None:
        try:
            return HTTPStatus(status_code).phrase
        except ValueError:
            return None

    @staticmethod
    def _retry_reason(exc: Exception) -> str:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            return "http_status_error"
        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.RemoteProtocolError):
            return "remote_protocol_error"
        if isinstance(exc, httpx.NetworkError):
            return "network_error"
        return "transport_error"

    def _retry_delay_seconds(
        self,
        *,
        attempt: int,
        response: Any | None = None,
    ) -> float:
        base_delay = self.retry_backoff_seconds * (2**attempt)
        server_hint_delay = self._retry_hint_seconds_from_response(response)
        delay_seconds = max(base_delay, server_hint_delay)
        if delay_seconds <= 0:
            return 0.0
        # Jitter avoids synchronized retries across concurrently running workers.
        jitter_seconds = random.uniform(0.0, delay_seconds * 0.25)
        return delay_seconds + jitter_seconds

    @classmethod
    def _retry_hint_seconds_from_response(cls, response: Any | None) -> float:
        if response is None:
            return 0.0
        headers = getattr(response, "headers", None)
        if headers is None:
            return 0.0
        candidates = (
            headers.get("Retry-After"),
            headers.get("RateLimit-Reset"),
            headers.get("X-RateLimit-Reset"),
        )
        parsed_candidates = [
            cls._parse_retry_hint_header_value(value)
            for value in candidates
            if isinstance(value, str) and value.strip()
        ]
        retry_hints = [value for value in parsed_candidates if value is not None]
        if not retry_hints:
            return 0.0
        return max(retry_hints)

    @staticmethod
    def _parse_retry_hint_header_value(value: str) -> float | None:
        raw_value = value.strip()
        if not raw_value:
            return None
        try:
            numeric = float(raw_value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw_value)
            except (TypeError, ValueError, OverflowError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, retry_at.timestamp() - time.time())
        if numeric <= 0:
            return 0.0
        # Some APIs emit absolute reset timestamps (seconds/ms), not delay seconds.
        if numeric >= 1_000_000_000_000:
            numeric = numeric / 1000.0
        if numeric >= 1_000_000_000:
            return max(0.0, numeric - time.time())
        return numeric


def _reasoning_text_fragments(delta: dict[str, Any]) -> list[str]:
    fragments: list[str] = []
    for key in ("reasoning", "reasoning_content"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            fragments.append(value)
    if fragments:
        return fragments
    for detail in delta.get("reasoning_details", []):
        fragments.extend(_extract_reasoning_strings(detail))
    return fragments


def _extract_reasoning_strings(value: Any) -> list[str]:
    fragments: list[str] = []
    if isinstance(value, str):
        if value:
            fragments.append(value)
        return fragments
    if isinstance(value, list):
        for item in value:
            fragments.extend(_extract_reasoning_strings(item))
        return fragments
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"text", "content", "summary", "reasoning", "reasoning_content"}:
                fragments.extend(_extract_reasoning_strings(item))
        return fragments
    return fragments


def _merge_reasoning_details(
    merged: dict[int, dict[str, Any]],
    detail_deltas: Any,
) -> None:
    if not isinstance(detail_deltas, list):
        return
    for fallback_index, delta in enumerate(detail_deltas):
        if not isinstance(delta, dict):
            continue
        index = int(delta.get("index", fallback_index))
        current = merged.setdefault(index, {})
        merged[index] = _merge_stream_value(current, delta)


def _merge_stream_value(current: Any, delta: Any, field_name: str | None = None) -> Any:
    if isinstance(current, dict) and isinstance(delta, dict):
        merged = dict(current)
        for key, value in delta.items():
            if key == "index":
                continue
            if key in merged:
                merged[key] = _merge_stream_value(merged[key], value, field_name=key)
            else:
                merged[key] = value
        return merged
    if isinstance(current, list) and isinstance(delta, list):
        merged_list = list(current)
        for index, value in enumerate(delta):
            if index < len(merged_list):
                merged_list[index] = _merge_stream_value(merged_list[index], value, field_name=field_name)
            else:
                merged_list.append(value)
        return merged_list
    if isinstance(current, str) and isinstance(delta, str):
        if field_name in {"text", "content", "summary", "reasoning", "reasoning_content"}:
            return current + delta
        return delta
    return delta


def _final_response_object(stream_object: str | None) -> str:
    if stream_object == "chat.completion.chunk":
        return "chat.completion"
    return stream_object or "chat.completion"


def _should_retry_empty_stream_response(
    *,
    content: str,
    reasoning: str,
    tool_calls: list[ToolCall],
    response_error: dict[str, Any] | None,
    finish_reason: str | None,
    native_finish_reason: str | None,
    attempt: int,
    max_attempts: int,
) -> bool:
    if attempt >= max_attempts - 1:
        return False
    if response_error is not None:
        return False
    if content.strip():
        return False
    if reasoning.strip():
        return False
    if tool_calls:
        return False
    if (finish_reason or "").strip():
        return False
    if (native_finish_reason or "").strip():
        return False
    return True
