from __future__ import annotations

import importlib.util
import sys
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


def _load_script_module(script_path: Path, module_name: str):
    script_dir = str(script_path.parent)
    sys.path.insert(0, script_dir)
    try:
        spec = importlib.util.spec_from_file_location(module_name, script_path)
        if spec is None or spec.loader is None:
            raise AssertionError(f"Unable to load script module: {script_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class SkillInstallerScriptTests(unittest.TestCase):
    def test_normalize_skill_rewrites_legacy_metadata(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script_path = (
            repo_root
            / "skills"
            / "skill-installer"
            / "resources"
            / "scripts"
            / "install-skill-from-github.py"
        )
        module = _load_script_module(script_path, "test_install_skill_from_github")

        with TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "demo-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "\n".join(
                    [
                        "---",
                        "name: Legacy Name",
                        "description: Legacy description",
                        "---",
                        "",
                        "# Body",
                        "",
                        "Use this skill.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (skill_dir / "skill.toml").write_text(
                "\n".join(
                    [
                        'id = "old-id"',
                        'name = "Legacy Name"',
                        'tags = []',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (skill_dir / "scripts").mkdir()
            (skill_dir / "scripts" / "helper.py").write_text("print('ok')\n", encoding="utf-8")
            (skill_dir / "agents").mkdir()

            warnings = module._normalize_skill(skill_dir, skill_name="demo-skill")

            self.assertTrue(any("Normalized metadata id" in item for item in warnings))
            self.assertTrue(any("Missing tags metadata" in item for item in warnings))
            self.assertFalse((skill_dir / "scripts").exists())
            self.assertFalse((skill_dir / "agents").exists())
            self.assertTrue((skill_dir / "resources" / "scripts" / "helper.py").is_file())

            metadata = tomllib.loads((skill_dir / "skill.toml").read_text(encoding="utf-8"))
            self.assertIn("skill", metadata)
            skill_meta = metadata["skill"]
            self.assertEqual(skill_meta["id"], "demo-skill")
            self.assertEqual(skill_meta["name"], "Legacy Name")
            self.assertEqual(skill_meta["name_cn"], "Legacy Name")
            self.assertEqual(skill_meta["description"], "Legacy description")
            self.assertEqual(skill_meta["tags"], ["imported"])

            skill_doc = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            self.assertFalse(skill_doc.lstrip().startswith("---"))


class SkillCreatorValidatorScriptTests(unittest.TestCase):
    def test_validate_skill_requires_skill_table(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script_path = (
            repo_root
            / "skills"
            / "skill-creator"
            / "resources"
            / "scripts"
            / "quick_validate.py"
        )
        module = _load_script_module(script_path, "test_quick_validate")

        with TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "demo-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
            (skill_dir / "skill.toml").write_text(
                "\n".join(
                    [
                        'id = "demo-skill"',
                        'name = "Demo Skill"',
                        'name_cn = "演示 Skill"',
                        'description = "desc"',
                        'description_cn = "描述"',
                        'tags = ["x"]',
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            errors = module.validate_skill(skill_dir)
            self.assertIn("skill.toml must contain a [skill] table", errors)

    def test_validate_skill_rejects_empty_tags(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script_path = (
            repo_root
            / "skills"
            / "skill-creator"
            / "resources"
            / "scripts"
            / "quick_validate.py"
        )
        module = _load_script_module(script_path, "test_quick_validate_empty_tags")

        with TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "demo-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
            (skill_dir / "skill.toml").write_text(
                "\n".join(
                    [
                        "[skill]",
                        'id = "demo-skill"',
                        'name = "Demo Skill"',
                        'name_cn = "演示 Skill"',
                        'description = "desc"',
                        'description_cn = "描述"',
                        "tags = []",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            errors = module.validate_skill(skill_dir)
            self.assertIn(
                "Metadata field 'tags' must be a non-empty list of strings",
                errors,
            )

