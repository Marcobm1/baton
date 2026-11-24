from baton.github import Project, index_markdown, resolve_dependencies
from baton.status import parse


def make(pid: str, path: str, front: str) -> Project:
    text = f"---\n{front}\nupdated: 2026-11-24\n---\n"
    repo = "/".join(pid.split("/")[:2])
    return Project(pid, repo, path, "main", True, f"https://github.com/{repo}", None, parse(text))


def test_parse_depends_on_and_parent():
    s = parse("---\nname: A\nstatus: planned\nupdated: 2026-11-24\n"
              "depends_on: [IA local, Other]\nparent: Big\n---\n")
    assert s.depends_on == ["IA local", "Other"] and s.parent == "Big" and not s.problems
    one = parse("---\nname: A\nstatus: planned\nupdated: 2026-11-24\ndepends_on: IA local\n---\n")
    assert one.depends_on == ["IA local"]
    bad = parse("---\nname: A\nstatus: planned\nupdated: 2026-11-24\ndepends_on: {a: 1}\n---\n")
    assert bad.depends_on == [] and any("depends_on" in p for p in bad.problems)


def test_blocked_until_dependency_is_done():
    base = make("me/proyectos/ia-local", "projects/ia-local/STATUS.md", "name: IA local\nstatus: planned")
    child = make("me/proyectos/cuantizacion", "projects/cuantizacion/STATUS.md",
                 "name: Cuantización\nstatus: planned\ndepends_on: [ia local]")
    by_folder = make("me/proyectos/other", "projects/other/STATUS.md",
                     "name: Other\nstatus: planned\ndepends_on: [ia-local]")
    missing = make("me/x", "STATUS.md", "name: X\nstatus: active\ndepends_on: [Nope]")
    projects = [base, child, by_folder, missing]

    resolve_dependencies(projects)
    assert child.blocked_by == ["IA local"] and by_folder.blocked_by == ["IA local"]
    assert missing.blocked_by == ["Nope"] and not base.blocked
    assert "Bloqueado hasta completar: IA local" in index_markdown(projects, lang="es")

    base.status.status = "done"
    resolve_dependencies(projects)
    assert not child.blocked and not by_folder.blocked


def test_blocked_card_is_red(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from baton import github, web
    from baton.config import Settings

    proj = make("me/proyectos/cuantizacion", "projects/cuantizacion/STATUS.md",
                "name: Cuantización\nstatus: planned\nparent: IA local")
    proj.blocked_by = ["IA local"]

    async def fake_load(self, force=False):
        return [proj], []

    monkeypatch.setattr(github.Hub, "load", fake_load)
    app = web.create_app(Settings(data_dir=tmp_path, lang="es", github_token=""))
    with TestClient(app) as client:
        page = client.get("/").text
        assert "card prio-medium blocked" in page
        assert "Bloqueado: hace falta completar antes: IA local" in page
        assert "Subproyecto de IA local" in page
        detail = client.get("/p/me/proyectos/cuantizacion").text
        assert "Bloqueado: hace falta completar antes: IA local" in detail
