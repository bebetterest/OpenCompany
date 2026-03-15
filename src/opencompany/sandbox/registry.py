from __future__ import annotations

from typing import TypeAlias

from opencompany.config import SandboxConfig
from opencompany.sandbox.anthropic import AnthropicSandboxBackend
from opencompany.sandbox.base import SandboxBackend
from opencompany.sandbox.none import NoSandboxBackend

SandboxBackendClass: TypeAlias = type[SandboxBackend]

_BACKEND_REGISTRY: dict[str, SandboxBackendClass] = {
    "anthropic": AnthropicSandboxBackend,
    "none": NoSandboxBackend,
}


def register_sandbox_backend(name: str, backend_cls: SandboxBackendClass) -> None:
    normalized = str(name or "").strip().lower()
    if not normalized:
        raise ValueError("Sandbox backend name must be non-empty.")
    _BACKEND_REGISTRY[normalized] = backend_cls


def available_sandbox_backends() -> tuple[str, ...]:
    return tuple(sorted(_BACKEND_REGISTRY.keys()))


def resolve_sandbox_backend_cls(config: SandboxConfig) -> SandboxBackendClass:
    backend_name = str(config.backend or "").strip().lower()
    if not backend_name:
        backend_name = "anthropic"
    backend_cls = _BACKEND_REGISTRY.get(backend_name)
    if backend_cls is None:
        supported = ", ".join(available_sandbox_backends()) or "<none>"
        raise ValueError(
            f"Unsupported sandbox backend '{backend_name}'. Supported backends: {supported}."
        )
    return backend_cls
