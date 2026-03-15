from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


_AGENT_PROMPT_BASES = {
    "root": "root_coordinator",
    "worker": "worker",
}

_JSON_PROMPT_BASES = {
    "runtime_messages": "runtime_messages",
    "tool_definitions": "tool_definitions",
}


def default_prompts_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "prompts"


def _localized_filename(base: str, locale: str, suffix: str) -> str:
    if locale == "zh":
        return f"{base}_cn{suffix}"
    return f"{base}{suffix}"


class PromptLibrary:
    def __init__(self, prompts_dir: Path) -> None:
        self.prompts_dir = prompts_dir
        self._fallback_prompts_dir = default_prompts_dir().resolve()
        self._text_cache: dict[tuple[str, str], str] = {}
        self._json_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def load(self, role: str, locale: str) -> str:
        return self.load_agent_prompt(role, locale)

    def load_agent_prompt(self, role: str, locale: str) -> str:
        base = _AGENT_PROMPT_BASES[role]
        return self._load_text(base, locale)

    def load_runtime_messages(self, locale: str) -> dict[str, str]:
        payload = self._load_json(_JSON_PROMPT_BASES["runtime_messages"], locale)
        return {key: str(value) for key, value in payload.items()}

    def load_tool_definitions(self, locale: str) -> dict[str, dict[str, Any]]:
        payload = self._load_json(_JSON_PROMPT_BASES["tool_definitions"], locale)
        return {
            str(key): copy.deepcopy(value)
            for key, value in payload.items()
            if isinstance(value, dict)
        }

    def render_runtime_message(self, key: str, locale: str, **values: Any) -> str:
        template = self.load_runtime_messages(locale)[key]
        return template.format(**values)

    def _load_text(self, base: str, locale: str) -> str:
        cache_key = (base, self._normalize_locale(locale))
        cached = self._text_cache.get(cache_key)
        if cached is not None:
            return cached
        path = self._localized_path(base, cache_key[1], ".md")
        content = path.read_text(encoding="utf-8")
        self._text_cache[cache_key] = content
        return content

    def _load_json(self, base: str, locale: str) -> dict[str, Any]:
        cache_key = (base, self._normalize_locale(locale))
        cached = self._json_cache.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)
        path = self._localized_path(base, cache_key[1], ".json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Prompt payload {path} must be a JSON object.")
        self._json_cache[cache_key] = payload
        return copy.deepcopy(payload)

    def _localized_path(self, base: str, locale: str, suffix: str) -> Path:
        primary_dir = self.prompts_dir.resolve()
        candidates = [
            primary_dir / _localized_filename(base, locale, suffix),
            primary_dir / _localized_filename(base, "en", suffix),
        ]
        if primary_dir != self._fallback_prompts_dir:
            candidates.extend(
                [
                    self._fallback_prompts_dir / _localized_filename(base, locale, suffix),
                    self._fallback_prompts_dir / _localized_filename(base, "en", suffix),
                ]
            )
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[1]

    @staticmethod
    def _normalize_locale(locale: str | None) -> str:
        return "zh" if locale == "zh" else "en"
