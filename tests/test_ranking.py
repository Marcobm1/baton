import re
import subprocess
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from baton import delegate
from baton.__main__ import main
from baton.config import Settings
from baton.ranking import Ranking
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
    assert row["human"] is None and row["agent"] == 9 and row["evaluated"] == 1
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
        assert web.post(url, data=form, follow_redirects=False).status_code == 303
    with TestClient(create_app(settings)) as restarted:
        page = restarted.get(url).text
        assert "Checked the cases" in page and "Needs fixes" in page
        assert 'name="rework" value="4"' in page
        assert model in restarted.get("/ranking").text
    assert main(args) == 0  # Re-saving the agent rating preserves the human rating.
    row = Ranking(tmp_path).overview()[0][0]
    assert dict(row) == dict(model=model, evaluated=1, fulfillment=8, correctness=6,
                             rework=7, agent=9, human=5)


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
    assert jobs[0]["outcome"] == outcome and models[0]["evaluated"] == 0
