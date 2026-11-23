"""Find STATUS.md files in a GitHub account's repositories.

Discovery rules (default branch only):
  * a repository with STATUS.md at its root is one project;
  * a repository with projects/<slug>/STATUS.md files contributes one project
    per folder (useful for a "hub" repo holding projects that have no repo
    of their own yet).
Repositories with neither are ignored.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass
from typing import Any

import httpx

from . import status as status_mod
from .config import Settings

API = "https://api.github.com"


@dataclass
class Project:
    id: str
    repo: str
    path: str
    branch: str
    private: bool
    html_url: str
    pushed_at: dt.datetime | None
    status: status_mod.Status

    @property
    def stale(self) -> bool:
        """True when the repo changed after STATUS.md's 'updated' day."""
        if not self.pushed_at or not self.status.updated:
            return False
        return (self.pushed_at.date() - self.status.updated).days >= STALE_DAYS[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repo": self.repo,
            "path": self.path,
            "branch": self.branch,
            "private": self.private,
            "url": self.html_url,
            "pushed_at": self.pushed_at.isoformat() if self.pushed_at else None,
            "stale": self.stale,
            **self.status.to_dict(),
        }


STALE_DAYS = [1]  # set from Settings at load time


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHubError(RuntimeError):
    pass


class Hub:
    def __init__(self, settings: Settings):
        self.settings = settings
        STALE_DAYS[0] = max(1, settings.stale_days)
        self._cache: tuple[float, list[Project], list[str]] | None = None
        self._lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "baton-project-hub",
        }
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        return httpx.AsyncClient(base_url=API, headers=headers, timeout=20)

    async def _get(self, client: httpx.AsyncClient, url: str, **kw) -> httpx.Response:
        r = await client.get(url, **kw)
        if r.status_code in (401, 403) and "rate limit" in r.text.lower():
            raise GitHubError("GitHub API rate limit reached; add a token or wait.")
        if r.status_code == 401:
            raise GitHubError("GitHub rejected the token (401). Check GITHUB_TOKEN.")
        return r

    async def _list_repos(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        s = self.settings
        if s.repos:
            out = []
            for full in s.repos:
                r = await self._get(client, f"/repos/{full}")
                if r.status_code == 200:
                    out.append(r.json())
            return out

        owner = s.owner
        me = ""
        if s.github_token:
            r = await self._get(client, "/user")
            if r.status_code == 200:
                me = r.json().get("login", "")
        owner = owner or me
        if not owner:
            raise GitHubError("Set BATON_OWNER (your GitHub user) or a GITHUB_TOKEN.")

        if me and me.lower() == owner.lower():
            url, params = "/user/repos", {"affiliation": "owner", "per_page": 100}
        else:
            url, params = f"/users/{owner}/repos", {"per_page": 100, "type": "owner"}
        repos: list[dict[str, Any]] = []
        page = 1
        while True:
            r = await self._get(client, url, params={**params, "page": page})
            if r.status_code != 200:
                raise GitHubError(f"Could not list repositories ({r.status_code}).")
            batch = r.json()
            repos.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return repos

    async def _raw(self, client: httpx.AsyncClient, repo: str, path: str, ref: str) -> str | None:
        r = await self._get(
            client,
            f"/repos/{repo}/contents/{path}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw"},
        )
        return r.text if r.status_code == 200 else None

    async def _projects_in(self, client: httpx.AsyncClient, repo: dict[str, Any]) -> list[Project]:
        full, branch = repo["full_name"], repo.get("default_branch") or "main"
        r = await self._get(client, f"/repos/{full}/contents/", params={"ref": branch})
        if r.status_code != 200:
            return []
        root = {item["name"]: item["type"] for item in r.json()}
        found: list[tuple[str, str]] = []  # (id, path)
        if root.get("STATUS.md") == "file":
            found.append((full, "STATUS.md"))
        if root.get("projects") == "dir":
            r2 = await self._get(client, f"/repos/{full}/contents/projects", params={"ref": branch})
            if r2.status_code == 200:
                for item in r2.json():
                    if item["type"] == "dir":
                        found.append((f"{full}/{item['name']}", f"projects/{item['name']}/STATUS.md"))

        projects = []
        for pid, path in found:
            text = await self._raw(client, full, path, branch)
            if text is None:
                continue
            fallback = repo["name"] if path == "STATUS.md" else path.split("/")[1]
            folder = path.rsplit("/", 1)[0] if "/" in path else ""
            html = repo["html_url"] + (f"/tree/{branch}/{folder}" if folder else "")
            projects.append(
                Project(
                    id=pid,
                    repo=full,
                    path=path,
                    branch=branch,
                    private=bool(repo.get("private")),
                    html_url=html,
                    pushed_at=_parse_time(repo.get("pushed_at")),
                    status=status_mod.parse(text, fallback_name=fallback),
                )
            )
        return projects

    async def create_issue(self, repo: str, title: str, body: str) -> dict[str, Any]:
        """Open an issue (needs a token with Issues: write on that repo)."""
        if not self.settings.github_token:
            raise GitHubError("Creating tasks needs a GitHub token with Issues: write.")
        async with self._client() as client:
            r = await client.post(f"/repos/{repo}/issues", json={"title": title, "body": body})
        if r.status_code != 201:
            raise GitHubError(f"GitHub did not create the issue ({r.status_code}).")
        data = r.json()
        return {"number": data["number"], "url": data["html_url"]}

    async def agent_comments(self, repo: str, issue: int) -> list[str]:
        async with self._client() as client:
            response = await self._get(client, f"/repos/{repo}/issues/{issue}/comments",
                                       params={"sort": "created", "direction": "desc", "per_page": 100})
            if response.status_code != 200:
                raise GitHubError("Could not read the task's comments.")
            return [c["body"] for c in response.json()
                    if c["user"]["login"] in ("claude[bot]", "github-actions[bot]")]

    def open_draft_pr(self, repo: str, branch: str, base: str, title: str, body: str) -> str:
        """Open a draft pull request (synchronous: called from the task thread)."""
        if not self.settings.github_token:
            raise GitHubError("Opening a pull request needs a GitHub token.")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "baton-project-hub",
            "Authorization": f"Bearer {self.settings.github_token}",
        }
        r = httpx.post(f"{API}/repos/{repo}/pulls", headers=headers, timeout=30,
                       json={"title": title, "head": branch, "base": base, "body": body,
                             "draft": True})
        if r.status_code == 422 and "draft" in r.text.lower():
            # Draft pull requests aren't available on private repos of free plans.
            r = httpx.post(f"{API}/repos/{repo}/pulls", headers=headers, timeout=30,
                           json={"title": title, "head": branch, "base": base, "body": body})
        if r.status_code != 201:
            raise GitHubError(f"GitHub did not open the pull request ({r.status_code}).")
        return r.json()["html_url"]

    async def load(self, force: bool = False) -> tuple[list[Project], list[str]]:
        """Return (projects, errors). Cached for settings.cache_seconds."""
        async with self._lock:
            now = time.monotonic()
            if not force and self._cache and now - self._cache[0] < self.settings.cache_seconds:
                return self._cache[1], self._cache[2]
            errors: list[str] = []
            projects: list[Project] = []
            try:
                async with self._client() as client:
                    repos = await self._list_repos(client)
                    repos = [
                        r for r in repos
                        if not r.get("archived")
                        and (self.settings.include_forks or not r.get("fork"))
                        and r["full_name"].lower() not in self.settings.exclude
                        and r["name"].lower() not in self.settings.exclude
                    ]
                    results = await asyncio.gather(
                        *(self._projects_in(client, r) for r in repos), return_exceptions=True
                    )
                    for repo, res in zip(repos, results):
                        if isinstance(res, Exception):
                            errors.append(f"{repo['full_name']}: {res}")
                        else:
                            projects.extend(res)
            except (GitHubError, httpx.HTTPError) as exc:
                errors.append(str(exc) or exc.__class__.__name__)
            order = {"high": 0, "medium": 1, "low": 2}
            projects.sort(key=lambda p: (order.get(p.status.priority, 1), p.status.name.lower()))
            self._cache = (now, projects, errors)
            return projects, errors


def index_markdown(projects: list[Project], lang: str = "en") -> str:
    """A Markdown table of every project, for agents that read one repo at a time."""
    es = lang == "es"
    today = dt.date.today().isoformat()
    lines = [
        "# " + ("Índice de proyectos" if es else "Project index"),
        "",
        ("_Generado por Baton el " if es else "_Generated by Baton on ") + today
        + (". No lo edites a mano: cada proyecto se actualiza en su STATUS.md._" if es
           else ". Do not edit by hand: each project is updated in its own STATUS.md._"),
        "",
    ]
    groups = ["active", "planned", "paused", "done"]
    titles = {
        "active": "En curso" if es else "Active",
        "planned": "Sin empezar" if es else "Planned",
        "paused": "En pausa" if es else "Paused",
        "done": "Terminados" if es else "Done",
    }
    head = (
        "| Proyecto | Prioridad | Siguiente paso | Actualizado | STATUS.md |"
        if es else "| Project | Priority | Next step | Updated | STATUS.md |"
    )
    for g in groups:
        items = [p for p in projects if p.status.status == g]
        if not items:
            continue
        lines += [f"## {titles[g]}", "", head, "|---|---|---|---|---|"]
        for p in items:
            s = p.status
            link = f"https://github.com/{p.repo}/blob/{p.branch}/{p.path}"
            nxt = s.next.replace("|", "\\|").replace("\n", " ")
            upd = (s.updated.isoformat() if s.updated else "?") + (f" ({s.updated_by})" if s.updated_by else "")
            if p.stale:
                upd += " ⚠"
            lines.append(f"| **{s.name}** — {s.summary.replace('|', '/')} | {s.priority} | {nxt} | {upd} | [{p.repo}/{p.path}]({link}) |")
        lines.append("")
    if any(p.stale for p in projects):
        lines.append(
            "⚠ = " + ("el repo tiene cambios posteriores a la última actualización de su STATUS.md."
                      if es else "the repo changed after its STATUS.md was last updated.")
        )
    return "\n".join(lines) + "\n"
