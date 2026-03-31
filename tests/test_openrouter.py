from __future__ import annotations

import json
import os
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

from opencompany.llm.openrouter import (
    OPENROUTER_HEADER_APP_NAME,
    OPENROUTER_HEADER_SITE_URL,
    OpenRouterClient,
    SseParser,
)
from opencompany.models import ToolCall
from opencompany.protocol import extract_json_object, normalize_tool_calls


class OpenRouterParserTests(unittest.TestCase):
    def test_sse_parser_handles_chunked_events(self) -> None:
        parser = SseParser()
        first = parser.feed('data: {"choices":[{"delta":{"content":"Hel')
        self.assertEqual(first, [])
        second = parser.feed('lo"}}]}\n\ndata: [DONE]\n\n')
        self.assertEqual(second[0], '{"choices":[{"delta":{"content":"Hello"}}]}')
        self.assertEqual(second[1], "[DONE]")

    def test_extract_json_object_reads_fenced_json(self) -> None:
        payload = extract_json_object(
            "```json\n{\"actions\":[{\"type\":\"complete\"}]}\n```"
        )
        self.assertEqual(payload["actions"][0]["type"], "complete")

    def test_normalize_tool_calls_reads_json_arguments(self) -> None:
        actions = normalize_tool_calls(
            [
                ToolCall(
                    id="call-1",
                    name="shell",
                    arguments_json='{"command":"TODO"}',
                )
            ]
        )
        self.assertEqual(actions[0]["type"], "shell")
        self.assertEqual(actions[0]["command"], "TODO")
        self.assertEqual(actions[0]["_tool_call_id"], "call-1")


class FakeStreamResponse:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def __aenter__(self) -> "FakeStreamResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb

    def raise_for_status(self) -> None:
        return None

    async def aiter_text(self):
        for chunk in self._chunks:
            yield chunk


class FakeAsyncClient:
    last_request: dict[str, object] | None = None
    response_chunks: list[str] = []
    last_timeout: int | None = None

    def __init__(self, *, timeout: int) -> None:
        type(self).last_timeout = timeout

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb

    def stream(self, method: str, url: str, *, headers: dict[str, str], json: dict[str, object]):
        type(self).last_request = {
            "method": method,
            "url": url,
            "headers": headers,
            "json": json,
        }
        return FakeStreamResponse(type(self).response_chunks)


class OpenRouterClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeAsyncClient.last_request = None
        FakeAsyncClient.response_chunks = []
        FakeAsyncClient.last_timeout = None

    async def test_stream_chat_sends_tool_calling_fields(self) -> None:
        client = OpenRouterClient(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=45,
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "shell",
                    "description": "Search files",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"}
                        },
                        "required": ["command"],
                    },
                },
            }
        ]
        FakeAsyncClient.response_chunks = ["data: [DONE]\n\n"]

        with mock.patch("httpx.AsyncClient", FakeAsyncClient):
            result = await client.stream_chat(
                model="openai/gpt-4o-mini",
                messages=[{"role": "system", "content": "Use tools."}],
                temperature=0.2,
                max_tokens=256,
                tools=tools,
                tool_choice="auto",
                parallel_tool_calls=False,
            )

        self.assertEqual(result.content, "")
        self.assertEqual(FakeAsyncClient.last_timeout, 45)
        assert FakeAsyncClient.last_request is not None
        payload = FakeAsyncClient.last_request["json"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["model"], "openai/gpt-4o-mini")
        self.assertEqual(payload["messages"], [{"role": "system", "content": "Use tools."}])
        self.assertEqual(payload["tools"], tools)
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(payload["parallel_tool_calls"], False)
        self.assertEqual(payload["stream"], True)
        headers = FakeAsyncClient.last_request["headers"]
        assert isinstance(headers, dict)
        self.assertEqual(headers["X-Title"], OPENROUTER_HEADER_APP_NAME)
        self.assertEqual(headers["HTTP-Referer"], OPENROUTER_HEADER_SITE_URL)

    async def test_stream_chat_parses_streamed_tool_calls(self) -> None:
        client = OpenRouterClient(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=30,
        )
        FakeAsyncClient.response_chunks = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_123","function":{"name":"shell","arguments":"{\\"command\\":\\"TO"}}]}}]}\n\n',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"DO\\"}"}}]}}]}\n\n',
            "data: [DONE]\n\n",
        ]

        with mock.patch("httpx.AsyncClient", FakeAsyncClient):
            result = await client.stream_chat(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": "Find TODO"}],
                temperature=0.1,
                max_tokens=128,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "shell",
                            "description": "Search files",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "command": {"type": "string"}
                                },
                                "required": ["command"],
                            },
                        },
                    }
                ],
                tool_choice="auto",
                parallel_tool_calls=False,
            )

        self.assertEqual(result.content, "")
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].id, "call_123")
        self.assertEqual(result.tool_calls[0].name, "shell")
        self.assertEqual(result.tool_calls[0].arguments_json, '{"command":"TODO"}')

        actions = normalize_tool_calls(result.tool_calls)
        self.assertEqual(
            actions,
            [
                {
                    "type": "shell",
                    "_tool_call_id": "call_123",
                    "command": "TODO",
                }
            ],
        )

    async def test_stream_chat_writes_debug_request_response_jsonl(self) -> None:
        with TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "requests_responses.jsonl"
            client = OpenRouterClient(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                timeout_seconds=30,
                request_response_log_path=log_path,
            )
            FakeAsyncClient.response_chunks = [
                'data: {"id":"gen-123","choices":[{"delta":{"content":"Hello"}}]}\n\n',
                "data: [DONE]\n\n",
            ]

            with mock.patch("httpx.AsyncClient", FakeAsyncClient):
                result = await client.stream_chat(
                    model="openai/gpt-4o-mini",
                    messages=[{"role": "user", "content": "Say hello"}],
                    temperature=0.2,
                    max_tokens=64,
                )

            self.assertEqual(result.content, "Hello")
            self.assertTrue(log_path.exists())
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["request"]["method"], "POST")
            self.assertEqual(
                records[0]["request"]["url"],
                "https://openrouter.ai/api/v1/chat/completions",
            )
            self.assertEqual(records[0]["request"]["payload"]["model"], "openai/gpt-4o-mini")
            self.assertEqual(records[0]["response"]["content"], "Hello")

    async def test_stream_chat_concatenates_reasoning_into_result(self) -> None:
        client = OpenRouterClient(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=30,
        )
        FakeAsyncClient.response_chunks = [
            'data: {"id":"gen-123","provider":"OpenAI","model":"openai/gpt-4o-mini","object":"chat.completion.chunk","created":1741478400,"system_fingerprint":"fp_123","choices":[{"index":0,"delta":{"reasoning":"Step 1: ","reasoning_details":[{"type":"reasoning.text","text":"Step 1: "}]}}]}\n\n',
            'data: {"id":"gen-123","choices":[{"index":0,"native_finish_reason":"stop","finish_reason":"stop","delta":{"reasoning_content":"Step 2.","reasoning_details":[{"type":"reasoning.text","text":"Step 2."}],"content":"Done"}}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n',
            "data: [DONE]\n\n",
        ]

        with mock.patch("httpx.AsyncClient", FakeAsyncClient):
            result = await client.stream_chat(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": "Explain"}],
                temperature=0.1,
                max_tokens=128,
            )

        self.assertEqual(result.content, "Done")
        self.assertEqual(result.reasoning, "Step 1: Step 2.")
        self.assertEqual(
            result.reasoning_details,
            [{"type": "reasoning.text", "text": "Step 1: Step 2."}],
        )
        self.assertEqual(result.response_id, "gen-123")
        self.assertEqual(result.provider, "OpenAI")
        self.assertEqual(result.model, "openai/gpt-4o-mini")
        self.assertEqual(result.object, "chat.completion.chunk")
        self.assertEqual(result.created, 1741478400)
        self.assertEqual(result.system_fingerprint, "fp_123")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.native_finish_reason, "stop")
        self.assertEqual(
            result.usage,
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    async def test_stream_chat_emits_reasoning_callback(self) -> None:
        client = OpenRouterClient(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=30,
        )
        FakeAsyncClient.response_chunks = [
            'data: {"choices":[{"delta":{"reasoning":"Inspect "}}]}\n\n',
            'data: {"choices":[{"delta":{"reasoning_content":"repo"}}]}\n\n',
            "data: [DONE]\n\n",
        ]
        reasoning_tokens: list[str] = []

        async def on_reasoning(token: str) -> None:
            reasoning_tokens.append(token)

        with mock.patch("httpx.AsyncClient", FakeAsyncClient):
            result = await client.stream_chat(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": "Explain"}],
                temperature=0.1,
                max_tokens=128,
                on_reasoning=on_reasoning,
            )

        self.assertEqual(reasoning_tokens, ["Inspect ", "repo"])
        self.assertEqual(result.reasoning, "Inspect repo")

    async def test_stream_chat_retries_empty_stream_response_without_finish_signal(self) -> None:
        class _EmptyThenRecoverStream:
            def __init__(self, *, return_empty: bool) -> None:
                self.return_empty = return_empty

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def raise_for_status(self) -> None:
                return None

            async def aiter_text(self):
                if self.return_empty:
                    yield 'data: {"id":"gen-empty","choices":[{"index":0,"delta":{"content":""}}]}\n\n'
                    yield "data: [DONE]\n\n"
                    return
                yield (
                    'data: {"id":"gen-ok","choices":[{"index":0,"finish_reason":"stop",'
                    '"native_finish_reason":"stop","delta":{"content":"ok"}}]}\n\n'
                )
                yield "data: [DONE]\n\n"

        class _EmptyThenRecoverClient:
            attempts = 0

            def __init__(self, *, timeout: int) -> None:
                del timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def stream(
                self,
                method: str,
                url: str,
                *,
                headers: dict[str, str],
                json: dict[str, object],
            ):
                del method, url, headers, json
                type(self).attempts += 1
                return _EmptyThenRecoverStream(return_empty=type(self).attempts == 1)

        client = OpenRouterClient(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=30,
            max_retries=2,
            retry_backoff_seconds=0.0,
        )
        retries: list[dict[str, object]] = []

        async def _on_retry(payload: dict[str, object]) -> None:
            retries.append(payload)

        with mock.patch("httpx.AsyncClient", _EmptyThenRecoverClient):
            result = await client.stream_chat(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": "test"}],
                temperature=0.1,
                max_tokens=64,
                on_retry=_on_retry,
            )

        self.assertEqual(result.content, "ok")
        self.assertEqual(_EmptyThenRecoverClient.attempts, 2)
        self.assertEqual(len(retries), 1)
        self.assertEqual(str(retries[0].get("retry_reason", "")), "empty_stream_response")

    async def test_stream_chat_retries_remote_protocol_error_before_first_event(self) -> None:
        import httpx

        class _FailThenRecoverStream:
            def __init__(self, *, fail_on_enter: bool) -> None:
                self.fail_on_enter = fail_on_enter

            async def __aenter__(self):
                if self.fail_on_enter:
                    raise httpx.RemoteProtocolError("server closed connection")
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def raise_for_status(self) -> None:
                return None

            async def aiter_text(self):
                yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                yield "data: [DONE]\n\n"

        class _FailThenRecoverClient:
            attempts = 0

            def __init__(self, *, timeout: int) -> None:
                del timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def stream(
                self,
                method: str,
                url: str,
                *,
                headers: dict[str, str],
                json: dict[str, object],
            ):
                del method, url, headers, json
                type(self).attempts += 1
                return _FailThenRecoverStream(fail_on_enter=type(self).attempts == 1)

        client = OpenRouterClient(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=30,
            max_retries=2,
            retry_backoff_seconds=0.0,
        )
        with mock.patch("httpx.AsyncClient", _FailThenRecoverClient):
            result = await client.stream_chat(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": "test"}],
                temperature=0.1,
                max_tokens=64,
            )

        self.assertEqual(result.content, "ok")
        self.assertEqual(_FailThenRecoverClient.attempts, 2)

    async def test_stream_chat_retries_http_520_before_first_event(self) -> None:
        import httpx

        class _FailThenRecoverStream:
            def __init__(self, *, fail_with_520: bool) -> None:
                self.fail_with_520 = fail_with_520

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def raise_for_status(self) -> None:
                if not self.fail_with_520:
                    return None
                request = httpx.Request(
                    "POST",
                    "https://openrouter.ai/api/v1/chat/completions",
                )
                response = httpx.Response(520, request=request)
                raise httpx.HTTPStatusError(
                    "Server error '520 <none>' for url 'https://openrouter.ai/api/v1/chat/completions'",
                    request=request,
                    response=response,
                )

            async def aiter_text(self):
                yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                yield "data: [DONE]\n\n"

        class _FailThenRecoverClient:
            attempts = 0

            def __init__(self, *, timeout: int) -> None:
                del timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def stream(
                self,
                method: str,
                url: str,
                *,
                headers: dict[str, str],
                json: dict[str, object],
            ):
                del method, url, headers, json
                type(self).attempts += 1
                return _FailThenRecoverStream(fail_with_520=type(self).attempts == 1)

        client = OpenRouterClient(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=30,
            max_retries=2,
            retry_backoff_seconds=0.0,
        )
        with mock.patch("httpx.AsyncClient", _FailThenRecoverClient):
            result = await client.stream_chat(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": "test"}],
                temperature=0.1,
                max_tokens=64,
            )

        self.assertEqual(result.content, "ok")
        self.assertEqual(_FailThenRecoverClient.attempts, 2)

    async def test_stream_chat_retries_http_400_before_first_event(self) -> None:
        import httpx

        class _FailThenRecoverStream:
            def __init__(self, *, fail_with_400: bool) -> None:
                self.fail_with_400 = fail_with_400

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def raise_for_status(self) -> None:
                if not self.fail_with_400:
                    return None
                request = httpx.Request(
                    "POST",
                    "https://openrouter.ai/api/v1/chat/completions",
                )
                response = httpx.Response(400, request=request)
                raise httpx.HTTPStatusError(
                    "Client error '400 Bad Request' for url 'https://openrouter.ai/api/v1/chat/completions'",
                    request=request,
                    response=response,
                )

            async def aiter_text(self):
                yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                yield "data: [DONE]\n\n"

        class _FailThenRecoverClient:
            attempts = 0

            def __init__(self, *, timeout: int) -> None:
                del timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def stream(
                self,
                method: str,
                url: str,
                *,
                headers: dict[str, str],
                json: dict[str, object],
            ):
                del method, url, headers, json
                type(self).attempts += 1
                return _FailThenRecoverStream(fail_with_400=type(self).attempts == 1)

        client = OpenRouterClient(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=30,
            max_retries=2,
            retry_backoff_seconds=0.0,
        )
        with mock.patch("httpx.AsyncClient", _FailThenRecoverClient):
            result = await client.stream_chat(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": "test"}],
                temperature=0.1,
                max_tokens=64,
            )

        self.assertEqual(result.content, "ok")
        self.assertEqual(_FailThenRecoverClient.attempts, 2)

    async def test_stream_chat_reports_retry_status_via_callback(self) -> None:
        import httpx

        class _UnauthorizedThenRecoverStream:
            def __init__(self, *, fail_with_401: bool) -> None:
                self.fail_with_401 = fail_with_401

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def raise_for_status(self) -> None:
                if not self.fail_with_401:
                    return None
                request = httpx.Request(
                    "POST",
                    "https://openrouter.ai/api/v1/chat/completions",
                )
                response = httpx.Response(401, request=request)
                raise httpx.HTTPStatusError(
                    "Client error '401 Unauthorized' for url 'https://openrouter.ai/api/v1/chat/completions'",
                    request=request,
                    response=response,
                )

            async def aiter_text(self):
                yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                yield "data: [DONE]\n\n"

        class _UnauthorizedThenRecoverClient:
            attempts = 0

            def __init__(self, *, timeout: int) -> None:
                del timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def stream(
                self,
                method: str,
                url: str,
                *,
                headers: dict[str, str],
                json: dict[str, object],
            ):
                del method, url, headers, json
                type(self).attempts += 1
                return _UnauthorizedThenRecoverStream(fail_with_401=type(self).attempts == 1)

        retries: list[dict[str, object]] = []

        async def _on_retry(payload: dict[str, object]) -> None:
            retries.append(payload)

        client = OpenRouterClient(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=30,
            max_retries=2,
            retry_backoff_seconds=0.0,
        )
        with mock.patch("httpx.AsyncClient", _UnauthorizedThenRecoverClient):
            result = await client.stream_chat(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": "test"}],
                temperature=0.1,
                max_tokens=64,
                on_retry=_on_retry,
            )

        self.assertEqual(result.content, "ok")
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0].get("status_code"), 401)
        self.assertEqual(retries[0].get("status_text"), "Unauthorized")
        self.assertEqual(retries[0].get("retry_reason"), "http_status_error")

    def test_retry_delay_seconds_prefers_server_retry_hint(self) -> None:
        import httpx

        client = OpenRouterClient(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=30,
            retry_backoff_seconds=1.0,
        )
        response = httpx.Response(429, headers={"Retry-After": "5"})
        with mock.patch("opencompany.llm.openrouter.random.uniform", return_value=0.0):
            self.assertEqual(
                client._retry_delay_seconds(attempt=2, response=response),
                5.0,
            )

    async def test_stream_chat_retries_http_429_with_retry_after_header(self) -> None:
        import httpx

        class _RateLimitedThenRecoverStream:
            def __init__(self, *, fail_with_429: bool) -> None:
                self.fail_with_429 = fail_with_429

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def raise_for_status(self) -> None:
                if not self.fail_with_429:
                    return None
                request = httpx.Request(
                    "POST",
                    "https://openrouter.ai/api/v1/chat/completions",
                )
                response = httpx.Response(429, request=request, headers={"Retry-After": "2"})
                raise httpx.HTTPStatusError(
                    "Client error '429 Too Many Requests' for url 'https://openrouter.ai/api/v1/chat/completions'",
                    request=request,
                    response=response,
                )

            async def aiter_text(self):
                yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                yield "data: [DONE]\n\n"

        class _RateLimitedThenRecoverClient:
            attempts = 0

            def __init__(self, *, timeout: int) -> None:
                del timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def stream(
                self,
                method: str,
                url: str,
                *,
                headers: dict[str, str],
                json: dict[str, object],
            ):
                del method, url, headers, json
                type(self).attempts += 1
                return _RateLimitedThenRecoverStream(fail_with_429=type(self).attempts == 1)

        sleep_calls: list[float] = []

        async def _fake_sleep(delay_seconds: float) -> None:
            sleep_calls.append(delay_seconds)

        client = OpenRouterClient(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=30,
            max_retries=2,
            retry_backoff_seconds=0.0,
        )
        with (
            mock.patch("httpx.AsyncClient", _RateLimitedThenRecoverClient),
            mock.patch("opencompany.llm.openrouter.asyncio.sleep", new=_fake_sleep),
            mock.patch("opencompany.llm.openrouter.random.uniform", return_value=0.0),
        ):
            result = await client.stream_chat(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": "test"}],
                temperature=0.1,
                max_tokens=64,
            )

        self.assertEqual(result.content, "ok")
        self.assertEqual(_RateLimitedThenRecoverClient.attempts, 2)
        self.assertEqual(sleep_calls, [2.0])

    async def test_stream_chat_does_not_retry_after_partial_stream_output(self) -> None:
        import httpx

        class _PartialThenDropStream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def raise_for_status(self) -> None:
                return None

            async def aiter_text(self):
                yield 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                raise httpx.RemoteProtocolError("server closed connection")

        class _PartialThenDropClient:
            attempts = 0

            def __init__(self, *, timeout: int) -> None:
                del timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def stream(
                self,
                method: str,
                url: str,
                *,
                headers: dict[str, str],
                json: dict[str, object],
            ):
                del method, url, headers, json
                type(self).attempts += 1
                return _PartialThenDropStream()

        client = OpenRouterClient(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=30,
            max_retries=2,
            retry_backoff_seconds=0.0,
        )
        with mock.patch("httpx.AsyncClient", _PartialThenDropClient):
            with self.assertRaises(httpx.RemoteProtocolError):
                await client.stream_chat(
                    model="openai/gpt-4o-mini",
                    messages=[{"role": "user", "content": "test"}],
                    temperature=0.1,
                    max_tokens=64,
                )

        self.assertEqual(_PartialThenDropClient.attempts, 1)

    async def test_stream_chat_debug_log_includes_partial_output_on_stream_failure(self) -> None:
        import httpx

        class _PartialThenDropStream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def raise_for_status(self) -> None:
                return None

            async def aiter_text(self):
                yield 'data: {"id":"gen-partial","choices":[{"delta":{"content":"partial"}}]}\n\n'
                raise httpx.RemoteProtocolError("server closed connection")

        class _PartialThenDropClient:
            def __init__(self, *, timeout: int) -> None:
                del timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb

            def stream(
                self,
                method: str,
                url: str,
                *,
                headers: dict[str, str],
                json: dict[str, object],
            ):
                del method, url, headers, json
                return _PartialThenDropStream()

        with TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "requests_responses.jsonl"
            client = OpenRouterClient(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                timeout_seconds=30,
                request_response_log_path=log_path,
            )
            with mock.patch("httpx.AsyncClient", _PartialThenDropClient):
                with self.assertRaises(httpx.RemoteProtocolError):
                    await client.stream_chat(
                        model="openai/gpt-4o-mini",
                        messages=[{"role": "user", "content": "test"}],
                        temperature=0.1,
                        max_tokens=64,
                    )

            self.assertTrue(log_path.exists())
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["request"]["payload"]["model"], "openai/gpt-4o-mini")
            self.assertEqual(records[0]["response"]["content"], "partial")
            self.assertTrue(bool(records[0]["response"]["stream"]["incomplete"]))
            self.assertEqual(records[0]["error"]["type"], "RemoteProtocolError")

    async def test_stream_chat_debug_log_uses_agent_and_module_scoped_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            debug_dir = Path(temp_dir) / "debug"
            client = OpenRouterClient(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                timeout_seconds=30,
            )
            client.request_response_log_dir = debug_dir
            FakeAsyncClient.response_chunks = [
                'data: {"id":"gen-123","choices":[{"delta":{"content":"Hello"}}]}\n\n',
                "data: [DONE]\n\n",
            ]

            with mock.patch("httpx.AsyncClient", FakeAsyncClient):
                result = await client.stream_chat(
                    model="openai/gpt-4o-mini",
                    messages=[{"role": "user", "content": "Say hello"}],
                    temperature=0.2,
                    max_tokens=64,
                    debug_agent_id="agent-Root/1",
                    debug_module="agent_runtime.ask",
                )

            self.assertEqual(result.content, "Hello")
            scoped_file = debug_dir / "agent-root_1__agent_runtime.ask.jsonl"
            self.assertTrue(scoped_file.exists())
            records = [
                json.loads(line)
                for line in scoped_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["scope"]["agent_id"], "agent-Root/1")
            self.assertEqual(records[0]["scope"]["module"], "agent_runtime.ask")
            self.assertEqual(records[0]["response"]["content"], "Hello")


@unittest.skipUnless(
    os.environ.get("OPENROUTER_RUN_INTEGRATION") == "1",
    "Set OPENROUTER_RUN_INTEGRATION=1 to run real OpenRouter integration tests.",
)
class OpenRouterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_chat_real_tool_call_round_trip(self) -> None:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            self.skipTest("OPENROUTER_API_KEY is required for real OpenRouter integration tests.")

        model = os.environ.get("OPENROUTER_TOOL_TEST_MODEL", "openai/gpt-4o-mini")
        base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        client = OpenRouterClient(
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=60,
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "shell",
                    "description": "Search a workspace for an exact string.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "Exact substring to search for.",
                            }
                        },
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

        result = await client.stream_chat(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a test model. Always comply with tool instructions. "
                        "Do not explain anything."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Call the shell tool with command exactly "
                        "'REAL_TOOL_TEST_42' and nothing else."
                    ),
                },
            ],
            temperature=0,
            max_tokens=128,
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "shell"}},
            parallel_tool_calls=False,
        )

        self.assertTrue(
            result.tool_calls,
            f"Expected a real tool call from model {model}, got content={result.content!r}",
        )
        self.assertEqual(result.tool_calls[0].name, "shell")

        actions = normalize_tool_calls(result.tool_calls)
        self.assertEqual(actions[0]["type"], "shell")
        self.assertEqual(actions[0]["command"], "REAL_TOOL_TEST_42")
        self.assertTrue(actions[0]["_tool_call_id"])
