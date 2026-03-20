from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx

from opencompany.config import McpServerConfig
from opencompany.utils import ensure_directory, stable_json_dumps, utc_now

_OAUTH_CALLBACK_PATH = "/oauth/callback"
_OAUTH_HTTP_TIMEOUT_SECONDS = 30.0
_OAUTH_REFRESH_LEEWAY_SECONDS = 300.0
_OAUTH_USER_AGENT = "OpenCompany/0.1.0"


class McpOAuthError(RuntimeError):
    pass


class McpOAuthRequiredError(McpOAuthError):
    pass


@dataclass(slots=True)
class McpOAuthMetadata:
    resource: str
    resource_metadata_url: str
    authorization_server: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str = ""
    scopes_supported: list[str] = field(default_factory=list)
    code_challenge_methods_supported: list[str] = field(default_factory=list)


@dataclass(slots=True)
class McpOAuthClientCredentials:
    client_id: str
    client_secret: str = ""


@dataclass(slots=True)
class McpOAuthSessionRecord:
    server_id: str
    server_url: str
    resource: str
    resource_metadata_url: str
    authorization_server: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str = ""
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    token_type: str = "Bearer"
    scope: str = ""
    expires_at: float | None = None
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "McpOAuthSessionRecord":
        expires_at_raw = payload.get("expires_at")
        expires_at: float | None = None
        if expires_at_raw not in {None, ""}:
            try:
                expires_at = float(expires_at_raw)
            except (TypeError, ValueError):
                expires_at = None
        return cls(
            server_id=str(payload.get("server_id", "") or "").strip(),
            server_url=str(payload.get("server_url", "") or "").strip(),
            resource=str(payload.get("resource", "") or "").strip(),
            resource_metadata_url=str(payload.get("resource_metadata_url", "") or "").strip(),
            authorization_server=str(payload.get("authorization_server", "") or "").strip(),
            issuer=str(payload.get("issuer", "") or "").strip(),
            authorization_endpoint=str(payload.get("authorization_endpoint", "") or "").strip(),
            token_endpoint=str(payload.get("token_endpoint", "") or "").strip(),
            registration_endpoint=str(payload.get("registration_endpoint", "") or "").strip(),
            client_id=str(payload.get("client_id", "") or "").strip(),
            client_secret=str(payload.get("client_secret", "") or "").strip(),
            access_token=str(payload.get("access_token", "") or "").strip(),
            refresh_token=str(payload.get("refresh_token", "") or "").strip(),
            token_type=str(payload.get("token_type", "Bearer") or "Bearer").strip()
            or "Bearer",
            scope=str(payload.get("scope", "") or "").strip(),
            expires_at=expires_at,
            updated_at=str(payload.get("updated_at", "") or "").strip() or utc_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "server_url": self.server_url,
            "resource": self.resource,
            "resource_metadata_url": self.resource_metadata_url,
            "authorization_server": self.authorization_server,
            "issuer": self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "registration_endpoint": self.registration_endpoint,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "scope": self.scope,
            "expires_at": self.expires_at,
            "updated_at": self.updated_at,
        }

    def is_expiring(self, *, leeway_seconds: float = _OAUTH_REFRESH_LEEWAY_SECONDS) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= (time.time() + max(0.0, leeway_seconds))


@dataclass(slots=True)
class McpOAuthLoginResult:
    record: McpOAuthSessionRecord
    authorization_url: str
    browser_opened: bool


class McpOAuthStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load_record(self, server_id: str) -> McpOAuthSessionRecord | None:
        payload = self._load_payload()
        record = payload.get("servers", {}).get(server_id)
        if not isinstance(record, dict):
            return None
        normalized = McpOAuthSessionRecord.from_dict(record)
        if not normalized.server_id:
            normalized.server_id = str(server_id or "").strip()
        return normalized

    def save_record(self, record: McpOAuthSessionRecord) -> None:
        payload = self._load_payload()
        servers = payload.setdefault("servers", {})
        assert isinstance(servers, dict)
        record.updated_at = utc_now()
        servers[record.server_id] = record.to_dict()
        self._write_payload(payload)

    def delete_record(self, server_id: str) -> bool:
        normalized_server_id = str(server_id or "").strip()
        if not normalized_server_id:
            return False
        payload = self._load_payload()
        servers = payload.setdefault("servers", {})
        assert isinstance(servers, dict)
        removed = normalized_server_id in servers
        if removed:
            servers.pop(normalized_server_id, None)
            self._write_payload(payload)
        return removed

    def _load_payload(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"servers": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - malformed file/runtime dependent
            raise McpOAuthError(
                f"Failed to read MCP OAuth token store: {self.path}"
            ) from exc
        if not isinstance(payload, dict):
            return {"servers": {}}
        servers = payload.get("servers")
        if not isinstance(servers, dict):
            payload["servers"] = {}
        return payload

    def _write_payload(self, payload: dict[str, Any]) -> None:
        ensure_directory(self.path.parent)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(stable_json_dumps(payload) + "\n", encoding="utf-8")
        _set_private_permissions(tmp_path)
        tmp_path.replace(self.path)
        _set_private_permissions(self.path)


class McpOAuthTokenProvider:
    _shared_locks: ClassVar[dict[tuple[str, str], asyncio.Lock]] = {}

    def __init__(
        self,
        *,
        server: McpServerConfig,
        store_path: Path,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self.server = server
        self.store = McpOAuthStore(store_path)
        self._http_client_factory = http_client_factory or _default_http_client
        self._lock = self._shared_lock(store_path=store_path, server_id=server.id)

    async def authorization_header(self) -> str:
        async with self._lock:
            record = self.store.load_record(self.server.id)
            if record is None or not record.access_token:
                raise McpOAuthRequiredError(
                    f"MCP server '{self.server.id}' requires OAuth login. "
                    f"Run `opencompany mcp-login --mcp-server {self.server.id}` first."
                )
            if record.is_expiring() and record.refresh_token:
                record = await self._refresh_record(record)
            if not record.access_token:
                raise McpOAuthRequiredError(
                    f"MCP server '{self.server.id}' requires OAuth login. "
                    f"Run `opencompany mcp-login --mcp-server {self.server.id}` first."
                )
            token_type = str(record.token_type or "").strip()
            if not token_type or token_type.lower() == "bearer":
                token_type = "Bearer"
            return f"{token_type} {record.access_token}"

    async def refresh_on_unauthorized(
        self,
        *,
        failed_authorization: str = "",
    ) -> bool:
        async with self._lock:
            record = self.store.load_record(self.server.id)
            if record is None or not record.refresh_token:
                return False
            failed_access_token = _extract_bearer_token(failed_authorization)
            if (
                failed_access_token
                and record.access_token
                and record.access_token != failed_access_token
            ):
                return True
            await self._refresh_record(record)
            return True

    @classmethod
    def _shared_lock(cls, *, store_path: Path, server_id: str) -> asyncio.Lock:
        key = (str(store_path.resolve()), str(server_id or "").strip())
        lock = cls._shared_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            cls._shared_locks[key] = lock
        return lock

    async def _refresh_record(self, record: McpOAuthSessionRecord) -> McpOAuthSessionRecord:
        if not record.refresh_token:
            raise McpOAuthRequiredError(
                f"MCP OAuth refresh token is missing for server '{self.server.id}'. "
                f"Run `opencompany mcp-login --mcp-server {self.server.id}` again."
            )
        params = {
            "grant_type": "refresh_token",
            "refresh_token": record.refresh_token,
            "client_id": record.client_id,
        }
        if self.server.oauth_use_resource_param and record.resource:
            params["resource"] = record.resource
        if record.client_secret:
            params["client_secret"] = record.client_secret
        async with self._http_client_factory() as client:
            response = await client.post(
                record.token_endpoint,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": _OAUTH_USER_AGENT,
                },
                content=urlencode(params),
            )
        payload = _parse_json_response(response, fallback_label="OAuth token refresh")
        if response.is_error:
            error_code = str(payload.get("error", "") or "").strip()
            if error_code == "invalid_grant":
                raise McpOAuthRequiredError(
                    f"MCP OAuth login expired for server '{self.server.id}'. "
                    f"Run `opencompany mcp-login --mcp-server {self.server.id}` again."
                )
            if error_code == "invalid_client":
                raise McpOAuthError(
                    f"MCP OAuth client credentials are invalid for server '{self.server.id}'."
                )
            detail = _oauth_error_detail(payload, response)
            raise McpOAuthError(
                f"Failed to refresh MCP OAuth token for server '{self.server.id}': {detail}"
            )
        record.access_token = str(payload.get("access_token", "") or "").strip()
        if not record.access_token:
            raise McpOAuthError(
                f"OAuth refresh for server '{self.server.id}' returned no access_token."
            )
        refreshed_token = str(payload.get("refresh_token", "") or "").strip()
        if refreshed_token:
            record.refresh_token = refreshed_token
        token_type = str(payload.get("token_type", "") or "").strip()
        if token_type:
            record.token_type = token_type
        scope = str(payload.get("scope", "") or "").strip()
        if scope:
            record.scope = scope
        record.expires_at = _expires_at_from_payload(payload)
        self.store.save_record(record)
        return record


async def complete_mcp_oauth_login(
    *,
    server: McpServerConfig,
    store_path: Path,
    timeout_seconds: float = 300.0,
    open_browser: bool = True,
    browser_opener: Callable[[str], bool] | None = None,
    authorization_url_callback: Callable[[str], None] | None = None,
    http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    callback_server_factory: Callable[[], Any] | None = None,
) -> McpOAuthLoginResult:
    if server.transport != "streamable_http":
        raise McpOAuthError("OAuth is only supported for streamable_http MCP servers.")
    if not server.oauth_enabled:
        raise McpOAuthError(
            f"MCP server '{server.id}' is not configured for OAuth login."
        )
    resource = canonicalize_resource_url(server.url)
    http_client_factory = http_client_factory or _default_http_client
    metadata = await discover_oauth_metadata(
        resource,
        http_client_factory=http_client_factory,
    )
    normalized_timeout_seconds = max(1.0, float(timeout_seconds))
    store = McpOAuthStore(store_path)
    existing = store.load_record(server.id)
    callback_server_factory = callback_server_factory or _LoopbackCallbackServer

    async with callback_server_factory() as callback_server:
        redirect_uri = callback_server.redirect_uri
        credentials = await _resolve_client_credentials(
            server=server,
            metadata=metadata,
            existing=existing,
            redirect_uri=redirect_uri,
            http_client_factory=http_client_factory,
        )
        code_verifier = generate_code_verifier()
        state = generate_state()
        authorization_url = build_authorization_url(
            metadata=metadata,
            credentials=credentials,
            redirect_uri=redirect_uri,
            code_challenge=generate_code_challenge(code_verifier),
            state=state,
            scopes=server.oauth_scopes,
            prompt=server.oauth_authorization_prompt,
            use_resource_param=server.oauth_use_resource_param,
        )
        if authorization_url_callback is not None:
            with contextlib.suppress(Exception):
                authorization_url_callback(authorization_url)
        browser_opener = browser_opener or _default_browser_opener
        browser_opened = bool(browser_opener(authorization_url)) if open_browser else False
        try:
            callback_params = await callback_server.wait_for_callback(
                timeout_seconds=normalized_timeout_seconds
            )
        except TimeoutError as exc:
            raise McpOAuthError(
                f"OAuth login timed out for server '{server.id}' after {int(normalized_timeout_seconds)}s while waiting for callback. "
                "Complete authorization in the browser and try a higher timeout (for example --timeout-seconds 180)."
            ) from exc

    error_code = str(callback_params.get("error", "") or "").strip()
    if error_code:
        error_description = str(callback_params.get("error_description", "") or "").strip()
        detail = error_code
        if error_description:
            detail += f": {error_description}"
        raise McpOAuthError(f"OAuth login failed for server '{server.id}': {detail}")
    callback_state = str(callback_params.get("state", "") or "").strip()
    if callback_state != state:
        raise McpOAuthError("OAuth callback state did not match the login request.")
    code = str(callback_params.get("code", "") or "").strip()
    if not code:
        raise McpOAuthError("OAuth callback did not include an authorization code.")

    token_payload = await exchange_code_for_tokens(
        metadata=metadata,
        credentials=credentials,
        code=code,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        use_resource_param=server.oauth_use_resource_param,
        http_client_factory=http_client_factory,
    )
    access_token = str(token_payload.get("access_token", "") or "").strip()
    if not access_token:
        raise McpOAuthError("OAuth token exchange returned no access_token.")
    record = McpOAuthSessionRecord(
        server_id=server.id,
        server_url=resource,
        resource=metadata.resource,
        resource_metadata_url=metadata.resource_metadata_url,
        authorization_server=metadata.authorization_server,
        issuer=metadata.issuer,
        authorization_endpoint=metadata.authorization_endpoint,
        token_endpoint=metadata.token_endpoint,
        registration_endpoint=metadata.registration_endpoint,
        client_id=credentials.client_id,
        client_secret=credentials.client_secret,
        access_token=access_token,
        refresh_token=str(token_payload.get("refresh_token", "") or "").strip(),
        token_type=str(token_payload.get("token_type", "Bearer") or "Bearer").strip()
        or "Bearer",
        scope=str(token_payload.get("scope", "") or "").strip(),
        expires_at=_expires_at_from_payload(token_payload),
    )
    store.save_record(record)
    return McpOAuthLoginResult(
        record=record,
        authorization_url=authorization_url,
        browser_opened=browser_opened,
    )


async def discover_oauth_metadata(
    server_url: str,
    *,
    http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> McpOAuthMetadata:
    requested_resource = canonicalize_resource_url(server_url)
    http_client_factory = http_client_factory or _default_http_client
    errors: list[str] = []
    protected_resource: dict[str, Any] | None = None
    resource_metadata_url = ""
    async with http_client_factory() as client:
        for candidate in _metadata_candidates(
            requested_resource,
            suffix=".well-known/oauth-protected-resource",
        ):
            try:
                response = await client.get(candidate, headers={"Accept": "application/json"})
            except Exception as exc:
                errors.append(f"{candidate}: {exc}")
                continue
            if response.status_code == 404:
                continue
            try:
                payload = _parse_json_response(
                    response,
                    fallback_label="OAuth protected resource metadata",
                )
            except McpOAuthError as exc:
                errors.append(f"{candidate}: {exc}")
                continue
            if response.is_error:
                errors.append(f"{candidate}: {_oauth_error_detail(payload, response)}")
                continue
            protected_resource = payload
            resource_metadata_url = candidate
            break
        if protected_resource is None:
            detail = "; ".join(errors) if errors else "no protected resource metadata found"
            raise McpOAuthError(
                f"Failed to discover OAuth protected resource metadata for '{requested_resource}': {detail}"
            )
        authorization_servers = protected_resource.get("authorization_servers")
        if not isinstance(authorization_servers, list) or not authorization_servers:
            raise McpOAuthError(
                f"OAuth protected resource metadata for '{requested_resource}' did not expose authorization_servers."
            )
        authorization_server = canonicalize_resource_url(str(authorization_servers[0]))
        metadata_resource = str(protected_resource.get("resource", "") or "").strip()
        if metadata_resource:
            try:
                resource = canonicalize_resource_url(metadata_resource)
            except McpOAuthError:
                resource = requested_resource
        else:
            resource = requested_resource

        authorization_metadata: dict[str, Any] | None = None
        errors = []
        for candidate in _metadata_candidates(
            authorization_server,
            suffix=".well-known/oauth-authorization-server",
        ):
            try:
                response = await client.get(candidate, headers={"Accept": "application/json"})
            except Exception as exc:
                errors.append(f"{candidate}: {exc}")
                continue
            if response.status_code == 404:
                continue
            try:
                payload = _parse_json_response(
                    response,
                    fallback_label="OAuth authorization server metadata",
                )
            except McpOAuthError as exc:
                errors.append(f"{candidate}: {exc}")
                continue
            if response.is_error:
                errors.append(f"{candidate}: {_oauth_error_detail(payload, response)}")
                continue
            authorization_metadata = payload
            break
    if authorization_metadata is None:
        detail = "; ".join(errors) if errors else "no authorization server metadata found"
        raise McpOAuthError(
            f"Failed to discover OAuth authorization metadata for '{authorization_server}': {detail}"
        )
    authorization_endpoint = str(
        authorization_metadata.get("authorization_endpoint", "") or ""
    ).strip()
    token_endpoint = str(authorization_metadata.get("token_endpoint", "") or "").strip()
    if not authorization_endpoint or not token_endpoint:
        raise McpOAuthError(
            f"OAuth authorization metadata for '{authorization_server}' is missing required endpoints."
        )
    return McpOAuthMetadata(
        resource=resource,
        resource_metadata_url=resource_metadata_url,
        authorization_server=authorization_server,
        issuer=str(authorization_metadata.get("issuer", "") or "").strip(),
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        registration_endpoint=str(
            authorization_metadata.get("registration_endpoint", "") or ""
        ).strip(),
        scopes_supported=[
            str(item).strip()
            for item in authorization_metadata.get("scopes_supported", [])
            if str(item).strip()
        ]
        if isinstance(authorization_metadata.get("scopes_supported"), list)
        else [],
        code_challenge_methods_supported=[
            str(item).strip()
            for item in authorization_metadata.get("code_challenge_methods_supported", [])
            if str(item).strip()
        ]
        if isinstance(authorization_metadata.get("code_challenge_methods_supported"), list)
        else [],
    )


def build_authorization_url(
    *,
    metadata: McpOAuthMetadata,
    credentials: McpOAuthClientCredentials,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scopes: list[str],
    prompt: str = "",
    use_resource_param: bool = True,
) -> str:
    params = {
        "response_type": "code",
        "client_id": credentials.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    if prompt:
        params["prompt"] = prompt
    if use_resource_param and metadata.resource:
        params["resource"] = metadata.resource
    return f"{metadata.authorization_endpoint}?{urlencode(params)}"


async def exchange_code_for_tokens(
    *,
    metadata: McpOAuthMetadata,
    credentials: McpOAuthClientCredentials,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    use_resource_param: bool = True,
    http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> dict[str, Any]:
    http_client_factory = http_client_factory or _default_http_client
    params = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": credentials.client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if use_resource_param and metadata.resource:
        params["resource"] = metadata.resource
    if credentials.client_secret:
        params["client_secret"] = credentials.client_secret
    async with http_client_factory() as client:
        response = await client.post(
            metadata.token_endpoint,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": _OAUTH_USER_AGENT,
            },
            content=urlencode(params),
        )
    payload = _parse_json_response(response, fallback_label="OAuth token exchange")
    if response.is_error:
        detail = _oauth_error_detail(payload, response)
        raise McpOAuthError(f"OAuth token exchange failed: {detail}")
    return payload


def canonicalize_resource_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise McpOAuthError(f"Invalid MCP OAuth URL: {url!r}")
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if path == "/":
        path = ""
    return urlunsplit((scheme, netloc, path, "", ""))


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(32)


def generate_code_challenge(code_verifier: str) -> str:
    digest = sha256(code_verifier.encode("utf-8")).digest()
    return _base64url(digest)


def generate_state() -> str:
    return secrets.token_hex(32)


def _extract_bearer_token(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    parts = normalized.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return normalized


def parse_resource_metadata_url(header_value: str | None) -> str:
    normalized = str(header_value or "").strip()
    if not normalized:
        return ""
    lower = normalized.lower()
    marker = "resource_metadata="
    index = lower.find(marker)
    if index < 0:
        return ""
    value = normalized[index + len(marker) :].lstrip()
    if not value:
        return ""
    if value[0] == '"':
        end_index = 1
        escaped = False
        while end_index < len(value):
            char = value[end_index]
            if char == '"' and not escaped:
                return value[1:end_index]
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
            end_index += 1
        return ""
    for separator in (",", " "):
        separator_index = value.find(separator)
        if separator_index >= 0:
            return value[:separator_index].strip()
    return value.strip()


async def _resolve_client_credentials(
    *,
    server: McpServerConfig,
    metadata: McpOAuthMetadata,
    existing: McpOAuthSessionRecord | None,
    redirect_uri: str,
    http_client_factory: Callable[[], httpx.AsyncClient],
) -> McpOAuthClientCredentials:
    if server.oauth_client_id:
        return McpOAuthClientCredentials(
            client_id=server.oauth_client_id,
            client_secret=server.oauth_client_secret,
        )
    if existing is not None and existing.client_id:
        return McpOAuthClientCredentials(
            client_id=existing.client_id,
            client_secret=existing.client_secret,
        )
    if not metadata.registration_endpoint:
        raise McpOAuthError(
            f"MCP server '{server.id}' does not advertise dynamic client registration. "
            f"Set oauth_client_id in opencompany.toml."
        )
    request_payload = {
        "client_name": server.oauth_client_name or "OpenCompany MCP Client",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if server.oauth_client_uri:
        request_payload["client_uri"] = server.oauth_client_uri
    if server.oauth_scopes:
        request_payload["scope"] = " ".join(server.oauth_scopes)
    async with http_client_factory() as client:
        response = await client.post(
            metadata.registration_endpoint,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=request_payload,
        )
    payload = _parse_json_response(response, fallback_label="OAuth dynamic client registration")
    if response.is_error:
        detail = _oauth_error_detail(payload, response)
        raise McpOAuthError(f"OAuth client registration failed: {detail}")
    client_id = str(payload.get("client_id", "") or "").strip()
    if not client_id:
        raise McpOAuthError("OAuth client registration returned no client_id.")
    return McpOAuthClientCredentials(
        client_id=client_id,
        client_secret=str(payload.get("client_secret", "") or "").strip(),
    )


def _metadata_candidates(server_url: str, *, suffix: str) -> list[str]:
    normalized = canonicalize_resource_url(server_url)
    parsed = urlsplit(normalized)
    candidates: list[str] = []
    seen: set[str] = set()
    path = parsed.path.rstrip("/")
    for candidate_path in (
        f"{path}/{suffix}" if path else "",
        f"/{suffix}",
    ):
        normalized_path = candidate_path.replace("//", "/")
        if not normalized_path:
            continue
        candidate = urlunsplit(
            (parsed.scheme, parsed.netloc, normalized_path, "", "")
        )
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def _expires_at_from_payload(payload: dict[str, Any]) -> float | None:
    raw_expires_in = payload.get("expires_in")
    try:
        expires_in = float(raw_expires_in)
    except (TypeError, ValueError):
        return None
    if expires_in <= 0:
        return None
    return time.time() + expires_in


def _parse_json_response(response: httpx.Response, *, fallback_label: str) -> dict[str, Any]:
    if not response.content:
        return {}
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise McpOAuthError(
            f"{fallback_label} returned invalid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise McpOAuthError(f"{fallback_label} must return a JSON object.")
    return payload


def _oauth_error_detail(payload: dict[str, Any], response: httpx.Response) -> str:
    error = str(payload.get("error", "") or "").strip()
    description = str(payload.get("error_description", "") or "").strip()
    if error and description:
        return f"{response.status_code} {error}: {description}"
    if error:
        return f"{response.status_code} {error}"
    text = ""
    with contextlib.suppress(Exception):
        text = response.text.strip()
    if text:
        return f"{response.status_code} {text}"
    return str(response.status_code)


def _base64url(data: bytes) -> str:
    return (
        __import__("base64")
        .urlsafe_b64encode(data)
        .decode("ascii")
        .rstrip("=")
    )


def _default_browser_opener(url: str) -> bool:
    with contextlib.suppress(Exception):
        return bool(webbrowser.open(url, new=1, autoraise=True))
    return False


def _default_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_OAUTH_HTTP_TIMEOUT_SECONDS, follow_redirects=True)


def _set_private_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except Exception:  # pragma: no cover - platform dependent
        return


class _LoopbackCallbackServer:
    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._params: dict[str, str] = {}
        self._event = asyncio.Event()
        self._lock = asyncio.Lock()
        self.redirect_uri = ""

    async def __aenter__(self) -> "_LoopbackCallbackServer":
        self._server = await asyncio.start_server(self._handle_client, "127.0.0.1", 0)
        socket = self._server.sockets[0]
        assert socket is not None
        port = int(socket.getsockname()[1])
        self.redirect_uri = f"http://127.0.0.1:{port}{_OAUTH_CALLBACK_PATH}"
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def wait_for_callback(self, *, timeout_seconds: float) -> dict[str, str]:
        await asyncio.wait_for(self._event.wait(), timeout=max(1.0, timeout_seconds))
        return dict(self._params)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await reader.readline()
            while True:
                header_line = await reader.readline()
                if not header_line or header_line in {b"\r\n", b"\n"}:
                    break
            path = _request_path_from_line(request_line)
            parsed = urlsplit(path)
            params = {
                str(key): str(values[-1])
                for key, values in parse_qs(parsed.query).items()
                if values
            }
            async with self._lock:
                if not self._event.is_set():
                    self._params = params
                    self._event.set()
            body = _callback_html(params)
            raw_body = body.encode("utf-8")
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(raw_body)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii") + raw_body
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def _request_path_from_line(request_line: bytes) -> str:
    try:
        decoded = request_line.decode("utf-8", errors="replace").strip()
    except Exception:
        return "/"
    parts = decoded.split(" ")
    if len(parts) < 2:
        return "/"
    return parts[1] or "/"


def _callback_html(params: dict[str, str]) -> str:
    error_code = str(params.get("error", "") or "").strip()
    if error_code:
        detail = str(params.get("error_description", "") or "").strip()
        message = f"OAuth login failed: {error_code}"
        if detail:
            message += f" ({detail})"
    else:
        message = "OAuth login completed. You can return to OpenCompany."
    return (
        "<!doctype html>"
        "<html><head><meta charset='utf-8'><title>OpenCompany MCP Login</title></head>"
        "<body><p>"
        + _escape_html(message)
        + "</p></body></html>"
    )


def _escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
