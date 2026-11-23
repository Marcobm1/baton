"""Auto-merge: tests on GitHub, review by Claude Code or Codex when needed, merge.

GitHub is faked; git is real (a bare "origin" repository on disk), so the
pushes of the review's fixes are checked for real.
"""

import subprocess
import sys
import time

from baton import runner as runner_mod
from baton.runner import LocalRepos, Runner, Task, TaskStore, review_verdict

from test_runner import make_repo

PASSED = {"pending": [], "passed": ["test"], "failed": []}
FAILED = {"pending": [], "passed": [], "failed": ["test"]}
NONE = {"pending": [], "passed": [], "failed": []}


class FakeGitHub:
    def __init__(self, checks):
        self._checks = checks          # callable(sha) -> dict, or a fixed dict
        self.comments, self.merged = [], []

    def checks(self, repo, sha):
        return self._checks(sha) if callable(self._checks) else self._checks

    def comment(self, repo, number, body):
        self.comments.append((number, body))

    def merge(self, repo, number, sha):
        self.merged.append((number, sha))


def _review_script(verdict, fix=False):
    lines = ["import sys", "sys.stdin.read()"]
    if fix:
        lines.append("open('FIX.md', 'w').write('fixed by the reviewer')")
    report = f"Verdict: {verdict}\\nChecked the change and ran the tests."
    lines.append(f"print('BATON_REVIEW_BEGIN\\n{report}\\nBATON_REVIEW_END')")
    return "; ".join(lines)


def _runner(tmp_path, work, gh, review=None, quota=None):
    store = TaskStore(tmp_path / "data")
    r = Runner(store, LocalRepos(str(work.parent), ""), timeout=60, local_model="",
               local_provider="ollama", git_name="Baton", git_email="baton@localhost",
               open_pr=lambda *a: "https://github.com/x/demo/pull/9", github=gh,
               auto_merge=True, quota_left=quota or (lambda agent: None))
    r.ci_poll, r.ci_grace = 0.01, 0.05
    r.agents_available = lambda: {a: "" for a in runner_mod.AGENTS}
    author = "import sys; sys.stdin.read() if not sys.stdin.closed else None; open('DONE.md','w').write('x')"
    calls = []

    def agent_cmd(task, wt):
        calls.append(task.agent)
        is_review = len(calls) > 1
        return [sys.executable, "-c", review if is_review else author]

    r._agent_cmd = agent_cmd
    return store, r, calls


def _start(r, store, agent, tid="t1"):
    r.submit(Task(id=tid, repo="x/demo", title="Add DONE", details="please", agent=agent,
                 where="local", created_at=time.time(), branch=f"baton/{tid}", base="main",
                 model="opencode/x" if agent == "opencode" else ""))
    for _ in range(400):
        t = store.get(tid)
        if t and t.status not in ("queued", "running"):
            return t
        time.sleep(0.05)
    raise AssertionError("task did not finish")


def _remote_head(origin, branch):
    return subprocess.run(["git", "-C", str(origin), "rev-parse", branch], capture_output=True,
                          text=True, check=True).stdout.strip()


def test_codex_task_with_passing_tests_is_merged_without_review(tmp_path, monkeypatch):
    work, origin = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    gh = FakeGitHub(PASSED)
    store, r, calls = _runner(tmp_path, work, gh)
    t = _start(r, store, "codex")
    assert t.status == "done" and t.merged, t.message
    assert calls == ["codex"] and not t.reviewer and gh.comments == []
    assert gh.merged == [(9, _remote_head(origin, "baton/t1"))]   # exactly what was pushed
    assert "merged into main" in t.message and t.worktree == ""


def test_failing_tests_leave_the_pull_request_open(tmp_path, monkeypatch):
    work, _ = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    gh = FakeGitHub(FAILED)
    store, r, _ = _runner(tmp_path, work, gh)
    t = _start(r, store, "claude-code")
    assert t.status == "done" and not t.merged and gh.merged == []
    assert "tests failed" in t.message and t.pr_url


def test_opencode_work_is_reviewed_fixed_and_merged(tmp_path, monkeypatch):
    work, origin = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    shas = []

    def checks(sha):
        shas.append(sha)
        return PASSED

    gh = FakeGitHub(checks)
    quota = {"claude-code": 10.0, "codex": 70.0}.get       # Codex has more left
    store, r, calls = _runner(tmp_path, work, gh, review=_review_script("FIXED", fix=True), quota=quota)
    t = _start(r, store, "opencode")
    assert t.status == "done" and t.merged, t.message
    assert calls == ["opencode", "codex"] and t.reviewer == "codex"
    assert t.review.startswith("Verdict: FIXED")
    head = _remote_head(origin, "baton/t1")
    files = subprocess.run(["git", "-C", str(origin), "show", "--name-only", "--format=%s", head],
                           capture_output=True, text=True, check=True).stdout
    assert "Review fixes by Codex" in files and "FIX.md" in files
    assert len(set(shas)) == 2 and shas[-1] == head          # tests checked again on the fixes
    assert gh.merged == [(9, head)]
    assert gh.comments and "Review by Codex" in gh.comments[0][1]

    # The task page shows the review and that it was merged.
    from fastapi.testclient import TestClient
    from baton.config import Settings
    from baton.web import create_app
    html = TestClient(create_app(Settings(data_dir=tmp_path / "data", lang="es"))).get("/tasks/t1").text
    assert "Revisión de Codex" in html and "fusionada" in html


def test_reviewer_block_leaves_it_open(tmp_path, monkeypatch):
    work, _ = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    gh = FakeGitHub(PASSED)
    store, r, calls = _runner(tmp_path, work, gh, review=_review_script("BLOCK", fix=True))
    t = _start(r, store, "opencode")
    assert not t.merged and gh.merged == [] and "advised not to merge" in t.message
    assert calls == ["opencode", "claude-code"]               # unknown quotas: Claude first
    assert gh.comments                                          # the reasons are on the PR


def test_repo_without_tests_gets_the_other_agent_as_reviewer(tmp_path, monkeypatch):
    work, origin = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    gh = FakeGitHub(NONE)
    store, r, calls = _runner(tmp_path, work, gh, review=_review_script("APPROVE"))
    t = _start(r, store, "claude-code")
    assert t.merged, t.message
    assert calls == ["claude-code", "codex"] and t.reviewer == "codex"
    assert gh.merged == [(9, _remote_head(origin, "baton/t1"))]
    assert "This repository runs no tests on GitHub" in " ".join(t.steps)


def test_no_reviewer_with_quota_leaves_it_open(tmp_path, monkeypatch):
    work, _ = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    gh = FakeGitHub(PASSED)
    store, r, calls = _runner(tmp_path, work, gh, review=_review_script("APPROVE"),
                              quota={"claude-code": 0.0, "codex": 0.0}.get)
    t = _start(r, store, "opencode")
    assert not t.merged and calls == ["opencode"] and "quota" in t.message


def test_review_verdict_parsing():
    assert review_verdict("Verdict: APPROVE\nall good") == "APPROVE"
    assert review_verdict("**Verdict:** fixed\n- typo") == "FIXED"
    assert review_verdict("Summary\nVerdict: BLOCK") == "BLOCK"
    assert review_verdict("Verdict: maybe") == "" and review_verdict("") == ""
