"""Tests for Phase 3A modularization boundaries, app isolation, and stdlib rules."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from baton import agents, review_merge, routes, runner, task_store, web, worktree
from baton.config import Settings


def test_runner_and_web_modular_exports():
    # Focused modules expose their own primitives and runner re-exports them.
    assert runner.Task is task_store.Task
    assert runner.TaskStore is task_store.TaskStore
    assert runner.PRIORITIES is task_store.PRIORITIES
    assert runner.Cancelled is task_store.Cancelled
    assert runner.LeftOpen is task_store.LeftOpen
    assert runner.UpdateInProgress is task_store.UpdateInProgress
    assert runner.AGENTS is agents.AGENTS
    assert runner.OPENCODE_CONFIG is agents.OPENCODE_CONFIG
    assert runner.extract_summary is agents.extract_summary
    assert runner._remote_repo is worktree._remote_repo
    assert issubclass(runner.LocalRepos, worktree.LocalRepos)
    assert callable(review_merge.finish_pr)
    assert callable(review_merge.wait_for_tests)
    assert callable(review_merge.run_review)
    assert len(routes.ROUTERS) == 9


def test_create_app_instances_have_isolated_context(tmp_path):
    app1 = web.create_app(Settings(data_dir=tmp_path / "d1"))
    app2 = web.create_app(Settings(data_dir=tmp_path / "d2"))
    ctx1, ctx2 = app1.state.ctx, app2.state.ctx

    assert ctx1 is not ctx2
    assert ctx1.csrf_token != ctx2.csrf_token
    assert ctx1.login_csrf != ctx2.login_csrf
    assert ctx1.store.dir != ctx2.store.dir
    assert ctx1.agent_chats is not ctx2.agent_chats


def test_quotas_and_statusline_stay_stdlib_only():
    root = Path(__file__).resolve().parent.parent / "baton"
    stdlib = sys.stdlib_module_names
    for name in ("quotas.py", "statusline.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top in stdlib, f"{name} imported non-stdlib module {alias.name!r}"
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{name} must not have relative imports"
                top = (node.module or "").split(".")[0]
                assert top in stdlib, f"{name} imported non-stdlib module {node.module!r}"
