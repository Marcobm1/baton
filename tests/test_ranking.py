import json
import re
import subprocess
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from baton import delegate
from baton.__main__ import main
from baton.config import Settings
from baton.ranking import Ranking, parse_rating
from baton.runner import Task, TaskStore
from baton.web import create_app


def test_reviews_survive_restart(tmp_path, monkeypatch, capsys):
    settings = Settings(data_dir=tmp_path, lang="es")
    monkeypatch.delenv("BATON_DELEGATION_DIR", raising=False)
    monkeypatch.setenv("BATON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BATON_ENV_FILE", str(tmp_path / "no-env"))
    model = "opencode-go/example-v2"
    monkeypatch.setattr(delegate, "find_opencode", lambda: "fake")
    monkeypatch.setattr(delegate, "opencode_models", lambda: [model])
    monkeypatch.setattr(delegate.subprocess, "run", Mock(return_value=
                        subprocess.CompletedProcess([], 0, "<script>answer</script>", "")))
    assert main(["delegate", "-m", model, "--type", "test draft", "Cover parsing"]) == 0
    output = capsys.readouterr()
    job = re.search(r"Delegation #(\d+)", output.err)[1]
    assert output.out == "<script>answer</script>"
    args = ["rate", job, "--agent", "codex", "--fulfillment", "10", "--correctness", "7",
            "--rework", "10", "--comment", "Checked the cases"]
    assert main(args) == 0
    store = Ranking(tmp_path)
    pending = store.start(model, "summary", "Another job")
    store.finish(pending, "done", "Summary")
    row = store.overview()[0][0]
    assert row["human"] is None and row["agent"] == 7.5 and row["evaluated"] == 2
    url = f"/ranking/{job}"
    with TestClient(create_app(settings)) as web:
        page = web.get(url).text
        assert "Checked the cases" in page and "&lt;script&gt;answer" in page
        assert model in page and "test draft" in page and "Cover parsing" in page
        token = re.search(r'name="csrf" value="([^"]+)"', page)[1]
        form = dict(fulfillment=6, correctness=5, rework=4, comment="Needs fixes")
        assert web.post(url, data=form).status_code == 403
        form["csrf"] = token
        assert web.post(url, data=dict(form, rework=0)).status_code == 422
        saved = web.post(url, data=form, follow_redirects=False)
        assert saved.status_code == 303
        # Saving returns to the Ranking list, not the job page.
        assert saved.headers["location"] == "/ranking?origin=all&view=agent&saved=1"
    with TestClient(create_app(settings)) as restarted:
        page = restarted.get(url).text
        assert "Checked the cases" in page and "Needs fixes" in page
        assert 'name="rework" value="4"' in page
        assert model in restarted.get("/ranking").text
    assert main(args) == 0  # Re-saving the agent rating preserves the human rating.
    row = Ranking(tmp_path).overview()[0][0]
    assert dict(row) == dict(model=model, evaluated=2, fulfillment=8, correctness=6,
                             rework=7, agent=7.5, human=5, combined=7)


def test_worktree_reviews_collected(tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    central = Ranking(tmp_path / "service")
    with central.worktree_env({}, worktree, "claude-code") as env:
        monkeypatch.setenv("BATON_DELEGATION_DIR", env["BATON_DELEGATION_DIR"])
        local = delegate.cli_ranking()
        job = local.start("provider/model", "summary", "Summarise")
        local.finish(job, "done", "Result")
        local.review(job, "claude-code", 8, 9, 10, "Usable")
    assert not list(worktree.iterdir())
    details, reviews = central.detail(job)
    assert details["result"] == "Result"
    assert reviews["agent"]["reviewer"] == "claude-code" and "human" not in reviews


@pytest.mark.parametrize("completed,code,outcome", [
    (subprocess.CompletedProcess([], 1, "partial", "provider error"), 1, "failed"),
    (subprocess.TimeoutExpired([], 1), 124, "timeout"),
])
def test_failed_delegations(tmp_path, monkeypatch, completed, code, outcome):
    monkeypatch.setenv("BATON_DELEGATION_DIR", str(tmp_path))
    monkeypatch.setattr(delegate, "find_opencode", lambda: "fake")
    run = Mock(side_effect=completed) if isinstance(completed, Exception) else Mock(return_value=completed)
    monkeypatch.setattr(delegate.subprocess, "run", run)
    assert delegate.run("provider/model", "prompt", ".", check_model=False) == code
    models, jobs = Ranking(tmp_path).overview()
    assert jobs[0]["outcome"] == outcome and models[0]["agent"] == 2

@pytest.mark.parametrize("field,value", [("fulfillment", 0), ("correctness", 11), ("rework", True),
                                         ("comment", " "), ("comment", "x" * 501), ("rework", "8")])
def test_invalid_rating(field, value):
    rating = dict(fulfillment=10, correctness=9, rework=8, comment="Checked")
    rating[field] = value
    assert parse_rating(json.dumps(rating)) is None
    assert parse_rating("") is None


def test_echoed_rating_before_final_review_is_ignored():
    from baton.runner import extract_summary

    output = ('BATON_RATING_BEGIN\n{"fulfillment":10,"correctness":10,"rework":10,"comment":"Echo"}'
              '\nBATON_RATING_END\nBATON_REVIEW_BEGIN\nVerdict: APPROVE\nBATON_REVIEW_END')
    assert extract_summary(output, "BATON_RATING", after="BATON_REVIEW_END") == ""


def test_import_filter_and_restart(tmp_path):
    settings = Settings(data_dir=tmp_path, lang="es")
    rows = [dict(id=status, repo="x/demo", title=f"Task {status}", details="The assignment",
                 agent="local" if status == "cancelled" else "opencode", where="local",
                 status=status, model="provider/model", created_at=123, review="<old report>")
            for status in ("done", "no-changes", "failed", "cancelled")]
    (tmp_path / "tasks.json").write_text(json.dumps(rows), encoding="utf8")
    ranking = Ranking(tmp_path)
    sub = ranking.start("provider/model", "test draft", "Assignment")
    ranking.review(sub, "human", 2, 2, 2, "")
    web = TestClient(create_app(settings))
    jobs = ranking.overview("task")[1]
    assert {j["task_id"] for j in jobs} == {r["id"] for r in rows}
    assert all(j["human"] is None for j in jobs)
    assert {j["outcome"]: j["agent"] for j in jobs} == {
        "done": 6, "no-changes": None, "failed": 2, "cancelled": None}
    job = jobs[0]
    link = f"/ranking/{job['id']}"
    assert f'href="{link}#your-score">Valorar' in web.get(f"/tasks/{job['task_id']}").text
    detail = web.get(link).text
    assert "The assignment" in detail and "Sin fusionar" in detail and "&lt;old report&gt;" in detail
    ranking.review(job["id"], "human", 8, 8, 8, "Keep this")
    restarted = TestClient(create_app(settings))
    assert "Keep this" in restarted.get(link).text
    assert len(ranking.overview("task")[1]) == len(rows)
    for origin, average in [("all", 5), ("delegation", 2), ("task", 8)]:
        page = restarted.get(f"/ranking?origin={origin}").text
        assert ("test draft" in page) == (origin != "task")
        assert ("Task cancelled" in page) == (origin != "delegation")
        assert f"<td>{average}.0</td>" in page


def test_only_finished_tasks_count(tmp_path):
    store = TaskStore(tmp_path)
    task = Task("x", "owner/repo", "Example", "", "opencode", "local", 123)
    for where, agent, status in [("local", "opencode", "queued"), ("local", "local", "running"),
                                 ("cloud", "claude-code", "sent"), ("local", "codex", "running")]:
        task.where, task.agent, task.status = where, agent, status
        store.save(task)
    assert not store.ranking.overview()[1]
    task.status = "done"
    store.save(task)
    store.save(task)
    assert len(store.ranking.overview()[1]) == 1

    cloud = Task("issue", "owner/repo", "Cloud work", "", "claude-code", "cloud", 456,
                 status="sent", summary="Completed the requested check")
    store.save(cloud)
    assert {job["task_id"] for job in store.ranking.overview("task")[1]} == {"x", "issue"}

@pytest.mark.parametrize("view", ["agent", "human", "combined"])
def test_chart_and_old_task_reviews(tmp_path, view):
    store = TaskStore(tmp_path)
    task = Task("a", "owner/repo", "Legacy change", "", "claude-code", "local", 123,
                status="done", review="Verdict: FIXED", model="example")
    store.save(task)
    job = store.record_task(task)
    web = TestClient(create_app(Settings(data_dir=tmp_path, lang="en")))
    empty = web.get("/ranking?origin=task&view=combined").text
    assert "No scores for this view yet." in empty
    assert "Reviewer made fixes" in web.get(f"/ranking/{job}").text
    store.ranking.review(job, "human", 9, 9, 9, "")
    page = web.get(f"/ranking?origin=task&view={view}").text
    score = {"agent": 7, "human": 9, "combined": 8}[view]
    assert f'<strong>{score:.1f}<small>/10</small>' in page
    assert f'view={view}" aria-current="page"' in page
    assert f'href="/ranking/{job}?origin=task&amp;view={view}#your-score">Edit score' in page


def test_saved_review_returns_to_ranking_with_a_confirmation(tmp_path):
    settings = Settings(data_dir=tmp_path, lang="es")
    ranking = Ranking(tmp_path)
    job = ranking.start("provider/model", "test draft", "Assignment")
    ranking.finish(job, "done", "Result")
    web = TestClient(create_app(settings))
    # The rate link keeps the list's filter and view; the form echoes them back.
    page = web.get(f"/ranking/{job}?origin=task&view=human").text
    assert 'name="origin" value="task"' in page and 'name="view" value="human"' in page
    token = re.search(r'name="csrf" value="([^"]+)"', page)[1]
    saved = web.post(f"/ranking/{job}", follow_redirects=False, data=dict(
        csrf=token, fulfillment=7, correctness=8, rework=9, comment="",
        origin="task", view="human"))
    assert saved.status_code == 303
    assert saved.headers["location"] == "/ranking?origin=task&view=human&saved=1"
    # The list shows a one-time confirmation; a normal visit doesn't.
    back = web.get(saved.headers["location"]).text
    assert "Valoración guardada" in back and "data-toast" in back
    assert "history.replaceState" in back          # the toast never repeats on reload
    assert "Valoración guardada" not in web.get("/ranking").text
    assert ranking.overview("delegation")[0][0]["human"] == 8
