from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from opencompany.orchestrator import Orchestrator
from test_orchestrator import build_test_project


class OrchestratorFinishTests(unittest.TestCase):
    def test_initial_messages_focus_on_task_context_not_embedded_tool_contracts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            root_message = orchestrator._root_initial_message("Inspect this project")
            worker_message = orchestrator._worker_initial_message("Inspect docs", project_dir)

            self.assertIn("User task: Inspect this project", root_message)
            self.assertNotIn("Target project directory:", root_message)
            self.assertIn("tool_run_id", root_message)
            self.assertNotIn("Implemented tools and exact action shapes", root_message)
            self.assertNotIn('"type":"finish"', root_message)
            self.assertIn("Assigned instruction: Inspect docs", worker_message)
            self.assertIn(f"Workspace: {project_dir}", worker_message)
            self.assertIn("Permission boundary:", worker_message)
            self.assertIn("tool_run_id", worker_message)
            self.assertNotIn("Implemented tools and exact action shapes", worker_message)
            self.assertNotIn('"type":"shell"', worker_message)

    def test_initial_messages_follow_selected_locale(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="zh", app_dir=project_dir)

            root_message = orchestrator._root_initial_message("检查这个项目")
            worker_message = orchestrator._worker_initial_message("检查文档", project_dir)

            self.assertIn("用户任务：检查这个项目", root_message)
            self.assertNotIn("目标项目目录：", root_message)
            self.assertIn("tool_run_id", root_message)
            self.assertIn("分配给你的指令：检查文档", worker_message)
            self.assertIn(f"工作区：{project_dir}", worker_message)
            self.assertIn("权限边界：这个工作区是你唯一可写区域。", worker_message)
            self.assertIn("tool_run_id", worker_message)

    def test_worker_initial_message_redacts_real_project_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            instruction = (
                f"Create docs under {project_dir} and do not modify unrelated paths."
            )
            worker_message = orchestrator._worker_initial_message(instruction, project_dir)
            instruction_line = worker_message.splitlines()[0]

            self.assertNotIn(str(project_dir), instruction_line)
            self.assertIn("<target-project>", instruction_line)
