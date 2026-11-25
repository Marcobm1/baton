"""Auto-merge: tests on GitHub, review by Claude Code for OpenCode/local work, merge.

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

    def invalidate(self):
        self.invalidated = getattr(self, "invalidated", 0) + 1


def _review_script(verdict, fix=False, rating=None):
    lines = ["import sys", "sys.stdin.read()"]
    if fix:
        lines.append("open('FIX.md', 'w').write('fixed by the reviewer')")
    report = f"Verdict: {verdict}\\nChecked the change and ran the tests."
    lines.append(f"print('BATON_REVIEW_BEGIN\\n{report}\\nBATON_REVIEW_END')")
    if rating is not None:
        block = f"BATON_RATING_BEGIN\n{rating}\nBATON_RATING_END"
        lines.append(f"print({block!r})")
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
    modes = []

    def agent_cmd(task, wt, restricted=False):
        calls.append(task.agent)
        modes.append(restricted)
        is_review = len(calls) > 1
        return [sys.executable, "-c", review if is_review else author]

    r.modes = modes

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


def test_thermal_stop_during_review_keeps_pr_open(tmp_path, monkeypatch):
    work, _ = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    gh = FakeGitHub(PASSED)
    ready = tmp_path / "review-started"
    review = f"import pathlib, time; pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
    store, runner, _ = _runner(tmp_path, work, gh, review=review)
    runner.submit(Task(id="review", repo="x/demo", title="Review", details="", agent="opencode",
                       where="local", created_at=time.time(), branch="baton/review", base="main"))
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
    finally:
        runner.set_overheated(True)
    for _ in range(100):
        task = store.get("review")
        if task.status == "cancelled":
            break
        time.sleep(0.05)
    assert task.status == "cancelled" and task.worktree and task.pr_url
    assert not gh.merged


def test_opencode_work_is_reviewed_fixed_and_merged(tmp_path, monkeypatch):
    work, origin = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    shas = []

    def checks(sha):
        shas.append(sha)
        return PASSED

    gh = FakeGitHub(checks)
    quota = {"claude-code": 10.0, "codex": 70.0}.get       # Codex has more left: still Claude
    review = _review_script("FIXED", fix=True, rating=
                            '{"fulfillment":9,"correctness":8,"rework":7,"comment":"Added a fix"}')
    store, r, calls = _runner(tmp_path, work, gh, review=review, quota=quota)
    t = _start(r, store, "opencode")
    assert t.status == "done" and t.merged, t.message
    assert calls == ["opencode", "claude-code"] and t.reviewer == "claude-code"
    assert r.modes == [False, True]                          # the review runs in limited mode
    jobs = store.ranking.overview("task")[1]
    assert len(jobs) == 1
    details, reviews = store.ranking.detail(jobs[0]["id"])
    assert details["merged"] and details["task_id"] == t.id
    assert tuple(reviews["agent"][k] for k in ("reviewer", "fulfillment", "correctness", "rework",
                                              "comment")) == ("claude-code", 9, 8, 7, "Added a fix")
    assert t.review.startswith("Verdict: FIXED")
    head = _remote_head(origin, "baton/t1")
    files = subprocess.run(["git", "-C", str(origin), "show", "--name-only", "--format=%s", head],
                           capture_output=True, text=True, check=True).stdout
    assert "Review fixes by Claude Code" in files and "FIX.md" in files
    assert len(set(shas)) == 2 and shas[-1] == head          # tests checked again on the fixes
    assert gh.merged == [(9, head)]
    assert gh.invalidated == 1                               # the project list refreshes at once
    assert gh.comments and "Review by Claude Code" in gh.comments[0][1]

    # The task page shows the review and that it was merged. Without a
    # configured model the heading stays plain: no dangling separator.
    from fastapi.testclient import TestClient
    from baton.config import Settings
    from baton.web import create_app
    html = TestClient(create_app(Settings(data_dir=tmp_path / "data", lang="es"))).get("/tasks/t1").text
    assert "Revisión de Claude Code" in html and "fusionada" in html


def test_reviewer_block_leaves_it_open(tmp_path, monkeypatch):
    work, _ = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    gh = FakeGitHub(PASSED)
    store, r, calls = _runner(tmp_path, work, gh, review=_review_script("BLOCK", fix=True, rating="bad JSON"))
    t = _start(r, store, "opencode")
    job, = store.ranking.overview("task")[1]
    assert store.ranking.detail(job["id"])[1] == {}
    assert not t.merged and gh.merged == [] and "advised not to merge" in t.message
    assert calls == ["opencode", "claude-code"]               # Claude Code always reviews
    assert gh.comments                                          # the reasons are on the PR


def test_trusted_agents_merge_without_review_even_without_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    for agent in ("claude-code", "codex", "gemini"):
        base = tmp_path / agent
        base.mkdir()
        work, origin = make_repo(base)
        gh = FakeGitHub(NONE)
        store, r, calls = _runner(base, work, gh, review=_review_script("BLOCK"))
        t = _start(r, store, agent)
        assert t.merged, (agent, t.message)
        assert calls == [agent] and not t.reviewer and gh.comments == []
        assert gh.merged == [(9, _remote_head(origin, "baton/t1"))]
        assert "This repository runs no tests on GitHub" in " ".join(t.steps)


def test_local_ai_work_is_reviewed_by_claude_code(tmp_path, monkeypatch):
    work, _ = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    gh = FakeGitHub(NONE)
    store, r, calls = _runner(tmp_path, work, gh, review=_review_script("APPROVE"))
    r.local_model = "qwen"
    t = _start(r, store, "local")
    assert t.merged and calls == ["local", "claude-code"] and t.reviewer == "claude-code"


def test_no_reviewer_with_quota_leaves_it_open(tmp_path, monkeypatch):
    work, _ = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    gh = FakeGitHub(PASSED)
    store, r, calls = _runner(tmp_path, work, gh, review=_review_script("APPROVE"),
                              quota={"claude-code": 0.0, "codex": 90.0}.get)
    t = _start(r, store, "opencode")
    assert not t.merged and calls == ["opencode"] and "quota" in t.message


def test_review_verdict_parsing():
    assert review_verdict("Verdict: APPROVE\nall good") == "APPROVE"
    assert review_verdict("**Verdict:** fixed\n- typo") == "FIXED"
    assert review_verdict("Summary\nVerdict: BLOCK") == "BLOCK"
    assert review_verdict("Verdict: maybe") == "" and review_verdict("") == ""


def test_reviewer_uses_the_configured_model_and_effort(tmp_path, monkeypatch):
    work, origin = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    monkeypatch.setattr(runner_mod, "catalog", lambda agent: {
        "opus": {"label": "Opus 5.5", "efforts": ["low", "medium", "high"]}} if agent == "claude-code" else {})
    gh = FakeGitHub(lambda sha: PASSED)
    store, r, calls = _runner(tmp_path, work, gh, review=_review_script("APPROVE"),
                              quota={"claude-code": 80.0, "codex": 10.0}.get)
    r.review_models = {"claude-code": ("opus", "medium")}
    seen = []
    agent_cmd = r._agent_cmd
    r._agent_cmd = lambda task, wt, restricted=False: (seen.append((task.agent, task.model, task.effort)),
                                                        agent_cmd(task, wt, restricted))[1]
    t = _start(r, store, "opencode")
    assert t.merged and seen[1] == ("claude-code", "opus", "medium")
    assert t.reviewer_model == "opus" and t.reviewer_effort == "medium"
    assert any("Review by Claude Code (opus · medium)" in s for s in t.steps)

    # The task page shows the reviewer's model and effort next to its name.
    from fastapi.testclient import TestClient
    from baton.config import Settings
    from baton.web import create_app
    html = TestClient(create_app(Settings(data_dir=tmp_path / "data", lang="es"))).get("/tasks/t1").text
    assert "Revisión de Claude Code ·" in html and "opus · medium" in html

    # A model the CLI doesn't list falls back to the CLI default; a bad effort is dropped.
    r.review_models = {"claude-code": ("opus-9", "medium")}
    assert r.reviewer_model("claude-code") == ("", "")
    r.review_models = {"claude-code": ("opus", "turbo")}
    assert r.reviewer_model("claude-code") == ("opus", "")
    assert r.reviewer_model("codex") == ("", "")          # nothing configured


def test_reviewer_defaults_to_opus_with_medium_effort(monkeypatch):
    for key in ("BATON_REVIEW_CLAUDE_MODEL", "BATON_REVIEW_CLAUDE_EFFORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BATON_ENV_FILE", "does-not-exist.env")
    from baton.config import Settings
    s = Settings.from_env()
    assert (s.review_claude_model, s.review_claude_effort) == ("opus", "medium")
    assert (Settings().review_claude_model, Settings().review_claude_effort) == ("opus", "medium")
