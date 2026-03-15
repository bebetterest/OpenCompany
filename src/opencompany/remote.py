from __future__ import annotations

import base64
import hmac
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from opencompany.models import RemoteSessionConfig

REMOTE_SESSION_FILENAME = "remote_session.json"
REMOTE_KIND = "remote_ssh"
REMOTE_AUTH_MODES = frozenset({"key", "password"})
REMOTE_KNOWN_HOSTS_POLICIES = frozenset({"accept_new", "strict"})
REMOTE_OS_VALUES = frozenset({"linux"})
REMOTE_PASSWORD_SERVICE = "opencompany.remote.password.v1"
REMOTE_PASSWORD_LABEL = "opencompany remote session password"
REMOTE_PASSWORD_FALLBACK_DIRNAME = "secrets"
REMOTE_PASSWORD_FALLBACK_KEY_FILENAME = "remote_passwords.key"
REMOTE_PASSWORD_FALLBACK_STORE_FILENAME = "remote_passwords.json"
SSH_TARGET_RE = re.compile(
    r"^(?P<user>[A-Za-z_][A-Za-z0-9_.-]*)@(?P<host>[A-Za-z0-9_.-]+)(?::(?P<port>[0-9]{1,5}))?$"
)

try:
    import keyring as _keyring  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dependency
    _keyring = None


def session_remote_config_path(session_dir: Path) -> Path:
    return session_dir / REMOTE_SESSION_FILENAME


def parse_ssh_target(ssh_target: str) -> tuple[str, str, int | None]:
    normalized = str(ssh_target or "").strip()
    match = SSH_TARGET_RE.fullmatch(normalized)
    if match is None:
        raise ValueError("ssh_target must be in user@host[:port] format.")
    user = str(match.group("user") or "").strip()
    host = str(match.group("host") or "").strip()
    port_raw = str(match.group("port") or "").strip()
    port: int | None = None
    if port_raw:
        port = int(port_raw)
        if port <= 0 or port > 65535:
            raise ValueError("ssh_target port must be in 1..65535.")
    return user, host, port


def build_remote_password_ref(session_id: str, config: RemoteSessionConfig) -> str:
    normalized_session = str(session_id or "").strip()
    if not normalized_session:
        raise ValueError("session_id is required to build password_ref.")
    material = (
        f"{normalized_session}\n"
        f"{str(config.ssh_target or '').strip()}\n"
        f"{str(config.remote_dir or '').strip()}\n"
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"remote-password-{digest}"


def _normalized_password_ref(password_ref: str) -> str:
    normalized = str(password_ref or "").strip()
    if not normalized:
        raise ValueError("password_ref must be non-empty.")
    return normalized


def _keyring_set(password_ref: str, password: str) -> bool:
    if _keyring is None:
        return False
    try:
        _keyring.set_password(REMOTE_PASSWORD_SERVICE, password_ref, password)
        return True
    except Exception:  # pragma: no cover - backend/runtime dependent
        return False


def _keyring_get(password_ref: str) -> str | None:
    if _keyring is None:
        return None
    try:
        value = _keyring.get_password(REMOTE_PASSWORD_SERVICE, password_ref)
    except Exception:  # pragma: no cover - backend/runtime dependent
        return None
    return str(value) if value is not None else None


def _keyring_delete(password_ref: str) -> bool:
    if _keyring is None:
        return False
    try:
        _keyring.delete_password(REMOTE_PASSWORD_SERVICE, password_ref)
        return True
    except Exception:  # pragma: no cover - backend/runtime dependent
        return False


def _macos_security_set(password_ref: str, password: str) -> bool:
    if shutil.which("security") is None:
        return False
    command = [
        "security",
        "add-generic-password",
        "-a",
        password_ref,
        "-s",
        REMOTE_PASSWORD_SERVICE,
        "-l",
        REMOTE_PASSWORD_LABEL,
        "-w",
        password,
        "-U",
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )
    return completed.returncode == 0


def _macos_security_get(password_ref: str) -> str | None:
    if shutil.which("security") is None:
        return None
    command = [
        "security",
        "find-generic-password",
        "-a",
        password_ref,
        "-s",
        REMOTE_PASSWORD_SERVICE,
        "-w",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.rstrip("\r\n")


def _macos_security_delete(password_ref: str) -> bool:
    if shutil.which("security") is None:
        return False
    command = [
        "security",
        "delete-generic-password",
        "-a",
        password_ref,
        "-s",
        REMOTE_PASSWORD_SERVICE,
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )
    return completed.returncode == 0


def _linux_secret_tool_set(password_ref: str, password: str) -> bool:
    if shutil.which("secret-tool") is None:
        return False
    command = [
        "secret-tool",
        "store",
        "--label",
        REMOTE_PASSWORD_LABEL,
        "service",
        REMOTE_PASSWORD_SERVICE,
        "account",
        password_ref,
    ]
    completed = subprocess.run(
        command,
        input=password,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )
    return completed.returncode == 0


def _linux_secret_tool_get(password_ref: str) -> str | None:
    if shutil.which("secret-tool") is None:
        return None
    command = [
        "secret-tool",
        "lookup",
        "service",
        REMOTE_PASSWORD_SERVICE,
        "account",
        password_ref,
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.rstrip("\r\n")


def _linux_secret_tool_delete(password_ref: str) -> bool:
    if shutil.which("secret-tool") is None:
        return False
    command = [
        "secret-tool",
        "clear",
        "service",
        REMOTE_PASSWORD_SERVICE,
        "account",
        password_ref,
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )
    return completed.returncode == 0


def _fallback_base_dir() -> Path:
    override = str(os.getenv("OPENCOMPANY_HOME", "") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".opencompany"


def _fallback_store_dir() -> Path:
    return _fallback_base_dir() / REMOTE_PASSWORD_FALLBACK_DIRNAME


def _fallback_key_path() -> Path:
    return _fallback_store_dir() / REMOTE_PASSWORD_FALLBACK_KEY_FILENAME


def _fallback_store_path() -> Path:
    return _fallback_store_dir() / REMOTE_PASSWORD_FALLBACK_STORE_FILENAME


def _set_private_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except Exception:  # pragma: no cover - platform dependent
        return


def _load_or_create_fallback_key() -> bytes | None:
    key_path = _fallback_key_path()
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            data = key_path.read_bytes()
            if len(data) == 32:
                return data
            if data:
                decoded = base64.urlsafe_b64decode(data)
                if len(decoded) == 32:
                    return decoded
            return None
        key = os.urandom(32)
        tmp_path = key_path.with_suffix(f"{key_path.suffix}.tmp")
        tmp_path.write_bytes(key)
        _set_private_permissions(tmp_path)
        tmp_path.replace(key_path)
        _set_private_permissions(key_path)
        return key
    except Exception:  # pragma: no cover - filesystem/runtime dependent
        return None


def _load_fallback_store() -> dict[str, str]:
    path = _fallback_store_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # pragma: no cover - malformed file/runtime dependent
        return {}
    if not isinstance(payload, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in payload.items():
        key_text = str(key or "").strip()
        value_text = str(value or "").strip()
        if key_text and value_text:
            normalized[key_text] = value_text
    return normalized


def _save_fallback_store(payload: dict[str, str]) -> bool:
    path = _fallback_store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _set_private_permissions(tmp_path)
        tmp_path.replace(path)
        _set_private_permissions(path)
        return True
    except Exception:  # pragma: no cover - filesystem/runtime dependent
        return False


def _xor_stream_cipher(key: bytes, nonce: bytes, data: bytes) -> bytes:
    output = bytearray(len(data))
    cursor = 0
    counter = 0
    while cursor < len(data):
        block = hashlib.sha256(
            key + nonce + counter.to_bytes(8, byteorder="big", signed=False)
        ).digest()
        take = min(len(block), len(data) - cursor)
        for idx in range(take):
            output[cursor + idx] = data[cursor + idx] ^ block[idx]
        cursor += take
        counter += 1
    return bytes(output)


def _encrypt_password_fallback(key: bytes, password: str) -> str:
    nonce = os.urandom(16)
    plaintext = password.encode("utf-8")
    ciphertext = _xor_stream_cipher(key, nonce, plaintext)
    mac = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    blob = nonce + mac + ciphertext
    return base64.urlsafe_b64encode(blob).decode("ascii")


def _decrypt_password_fallback(key: bytes, encoded: str) -> str | None:
    try:
        blob = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except Exception:  # pragma: no cover - malformed data
        return None
    if len(blob) < 48:
        return None
    nonce = blob[:16]
    mac = blob[16:48]
    ciphertext = blob[48:]
    expected = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        return None
    plaintext = _xor_stream_cipher(key, nonce, ciphertext)
    try:
        return plaintext.decode("utf-8")
    except Exception:  # pragma: no cover - malformed data
        return None


def _fallback_store_set(password_ref: str, password: str) -> bool:
    key = _load_or_create_fallback_key()
    if key is None:
        return False
    encrypted = _encrypt_password_fallback(key, password)
    payload = _load_fallback_store()
    payload[password_ref] = encrypted
    return _save_fallback_store(payload)


def _fallback_store_get(password_ref: str) -> str | None:
    key = _load_or_create_fallback_key()
    if key is None:
        return None
    payload = _load_fallback_store()
    encrypted = str(payload.get(password_ref, "") or "").strip()
    if not encrypted:
        return None
    return _decrypt_password_fallback(key, encrypted)


def _fallback_store_delete(password_ref: str) -> bool:
    payload = _load_fallback_store()
    if password_ref not in payload:
        return False
    payload.pop(password_ref, None)
    return _save_fallback_store(payload)


def save_remote_session_password(password_ref: str, password: str) -> None:
    normalized_ref = _normalized_password_ref(password_ref)
    normalized_password = str(password or "")
    if not normalized_password:
        raise ValueError("password must be non-empty.")
    stored_in_backend = (
        _keyring_set(normalized_ref, normalized_password)
        or _macos_security_set(normalized_ref, normalized_password)
        or _linux_secret_tool_set(normalized_ref, normalized_password)
    )
    stored_in_fallback = _fallback_store_set(normalized_ref, normalized_password)
    if stored_in_backend or stored_in_fallback:
        return
    raise RuntimeError(
        "No secure local credential backend available for remote password storage "
        "(keyring/security/secret-tool/fallback-store all failed)."
    )


def load_remote_session_password(password_ref: str) -> str | None:
    normalized_ref = _normalized_password_ref(password_ref)
    value = _keyring_get(normalized_ref)
    if value:
        return value
    value = _macos_security_get(normalized_ref)
    if value:
        return value
    value = _linux_secret_tool_get(normalized_ref)
    if value:
        return value
    value = _fallback_store_get(normalized_ref)
    if value:
        return value
    return None


def delete_remote_session_password(password_ref: str) -> None:
    normalized_ref = _normalized_password_ref(password_ref)
    deleted = False
    deleted = _keyring_delete(normalized_ref) or deleted
    deleted = _macos_security_delete(normalized_ref) or deleted
    deleted = _linux_secret_tool_delete(normalized_ref) or deleted
    deleted = _fallback_store_delete(normalized_ref) or deleted
    if deleted:
        return


def normalize_remote_session_config(payload: Any) -> RemoteSessionConfig:
    if isinstance(payload, RemoteSessionConfig):
        config = payload
    elif isinstance(payload, dict):
        config = RemoteSessionConfig(
            kind=str(payload.get("kind", REMOTE_KIND) or REMOTE_KIND).strip().lower(),
            ssh_target=str(payload.get("ssh_target", "") or "").strip(),
            remote_dir=str(payload.get("remote_dir", "") or "").strip(),
            auth_mode=str(payload.get("auth_mode", "key") or "key").strip().lower(),
            identity_file=str(payload.get("identity_file", "") or "").strip(),
            known_hosts_policy=(
                str(payload.get("known_hosts_policy", "accept_new") or "accept_new")
                .strip()
                .lower()
            ),
            remote_os=str(payload.get("remote_os", "linux") or "linux").strip().lower(),
            password_ref=str(payload.get("password_ref", "") or "").strip(),
        )
    else:
        raise ValueError("Remote session config must be an object.")
    return validate_remote_session_config(config)


def validate_remote_session_config(config: RemoteSessionConfig) -> RemoteSessionConfig:
    if config.kind != REMOTE_KIND:
        raise ValueError(f"Unsupported remote config kind: {config.kind}")
    parse_ssh_target(config.ssh_target)
    remote_dir = str(config.remote_dir or "").strip()
    if not remote_dir.startswith("/"):
        raise ValueError("remote_dir must be an absolute Linux path.")
    if config.auth_mode not in REMOTE_AUTH_MODES:
        raise ValueError("auth_mode must be one of: key, password.")
    if config.auth_mode == "key":
        if not str(config.identity_file or "").strip():
            raise ValueError("identity_file is required when auth_mode=key.")
        if str(config.password_ref or "").strip():
            config.password_ref = ""
    else:
        config.identity_file = ""
        config.password_ref = str(config.password_ref or "").strip()
    if config.known_hosts_policy not in REMOTE_KNOWN_HOSTS_POLICIES:
        raise ValueError("known_hosts_policy must be one of: accept_new, strict.")
    if config.remote_os not in REMOTE_OS_VALUES:
        raise ValueError("remote_os must be linux for V1.")
    return config


def dump_remote_session_config(config: RemoteSessionConfig) -> dict[str, Any]:
    validated = validate_remote_session_config(config)
    return asdict(validated)


def load_remote_session_config(session_dir: Path) -> RemoteSessionConfig | None:
    path = session_remote_config_path(session_dir)
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("remote_session.json must contain an object.")
    return normalize_remote_session_config(payload)


def save_remote_session_config(session_dir: Path, config: RemoteSessionConfig) -> Path:
    path = session_remote_config_path(session_dir)
    path.write_text(
        json.dumps(dump_remote_session_config(config), ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def delete_remote_session_config(session_dir: Path) -> None:
    session_remote_config_path(session_dir).unlink(missing_ok=True)
