from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / "docs"
MODULE_DOCS_ROOT = DOCS_ROOT / "modules"
MCP_PRESET_IDS = ("huggingface", "notion", "github", "duckduckgo")


def _extract_markdown_heading_levels(path: Path) -> list[str]:
    levels: list[str] = []
    in_code_fence = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        matched = re.match(r"^(#{1,6})\s+\S", line)
        if matched is not None:
            levels.append(matched.group(1))
    return levels


def _extract_mcp_enabled_flags(path: Path) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    current_server: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        section_match = re.match(r"^\[mcp\.servers\.([^\]]+)\]$", line)
        if section_match is not None:
            current_server = section_match.group(1)
            continue
        if current_server is None:
            continue
        enabled_match = re.match(r"^enabled\s*=\s*(true|false)\s*$", line)
        if enabled_match is not None:
            flags[current_server] = enabled_match.group(1) == "true"
            current_server = None
    return flags


def _extract_project_default_locale(path: Path) -> str | None:
    in_project_section = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        section_match = re.match(r"^\[([^\]]+)\]$", line)
        if section_match is not None:
            in_project_section = section_match.group(1) == "project"
            continue
        if not in_project_section:
            continue
        locale_match = re.match(r'^default_locale\s*=\s*"([^"]+)"\s*$', line)
        if locale_match is not None:
            return locale_match.group(1)
    return None


class RepositoryContractTests(unittest.TestCase):
    def test_readme_mcp_example_matches_opencompany_defaults(self) -> None:
        config_flags = _extract_mcp_enabled_flags(REPO_ROOT / "opencompany.toml")
        readme_flags = _extract_mcp_enabled_flags(REPO_ROOT / "README.md")
        readme_cn_flags = _extract_mcp_enabled_flags(REPO_ROOT / "README_cn.md")

        for server_id in MCP_PRESET_IDS:
            self.assertIn(server_id, config_flags)
            self.assertIn(server_id, readme_flags)
            self.assertIn(server_id, readme_cn_flags)
            self.assertEqual(
                readme_flags[server_id],
                config_flags[server_id],
                msg=f"README.md MCP default drift for '{server_id}'",
            )
            self.assertEqual(
                readme_cn_flags[server_id],
                config_flags[server_id],
                msg=f"README_cn.md MCP default drift for '{server_id}'",
            )

    def test_default_locale_contract_matches_docs(self) -> None:
        config_default_locale = _extract_project_default_locale(REPO_ROOT / "opencompany.toml")
        self.assertEqual(config_default_locale, "auto")

        readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        readme_cn_text = (REPO_ROOT / "README_cn.md").read_text(encoding="utf-8")
        self.assertRegex(readme_text, r'\[project\]\.default_locale\s*=\s*"auto"')
        self.assertRegex(readme_cn_text, r'\[project\]\.default_locale\s*=\s*"auto"')

    def test_readme_heading_levels_match_chinese_mirror(self) -> None:
        self.assertEqual(
            _extract_markdown_heading_levels(REPO_ROOT / "README.md"),
            _extract_markdown_heading_levels(REPO_ROOT / "README_cn.md"),
        )

    def test_docs_index_heading_levels_match_chinese_mirror(self) -> None:
        self.assertEqual(
            _extract_markdown_heading_levels(DOCS_ROOT / "README.md"),
            _extract_markdown_heading_levels(DOCS_ROOT / "README_cn.md"),
        )

    def test_root_docs_have_chinese_mirrors_with_same_heading_levels(self) -> None:
        english_docs = sorted(
            path
            for path in DOCS_ROOT.glob("*.md")
            if path.name != "README.md" and not path.name.endswith("_cn.md")
        )
        self.assertTrue(english_docs)
        for english_path in english_docs:
            chinese_path = english_path.with_name(f"{english_path.stem}_cn.md")
            self.assertTrue(chinese_path.exists(), msg=f"Missing mirror: {chinese_path}")
            self.assertEqual(
                _extract_markdown_heading_levels(english_path),
                _extract_markdown_heading_levels(chinese_path),
                msg=f"Heading-level drift: {english_path.name} vs {chinese_path.name}",
            )

    def test_module_docs_have_chinese_mirrors_with_same_heading_levels(self) -> None:
        english_docs = sorted(
            path for path in MODULE_DOCS_ROOT.glob("*.md") if not path.name.endswith("_cn.md")
        )
        self.assertTrue(english_docs)
        for english_path in english_docs:
            chinese_path = english_path.with_name(f"{english_path.stem}_cn.md")
            self.assertTrue(chinese_path.exists(), msg=f"Missing mirror: {chinese_path}")
            self.assertEqual(
                _extract_markdown_heading_levels(english_path),
                _extract_markdown_heading_levels(chinese_path),
                msg=f"Heading-level drift: {english_path.name} vs {chinese_path.name}",
            )
