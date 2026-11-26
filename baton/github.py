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
import base64
import datetime as dt
import hashlib
import re
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from . import status as status_mod
from .project_detail import sections
from .config import Settings

API = "https://api.github.com"

# One page includes the rollup of both check runs and legacy commit statuses.
_OPEN_PULLS = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes { number title commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


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
    blocked_by: list[str] = field(default_factory=list)

    @property
    def status_sha(self) -> str:
        data = self.status.source.encode("utf-8")
        return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_by)

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
            "blocked_by": self.blocked_by,
            **self.status.to_dict(),
        }


STALE_DAYS = [1]  # set from Settings at load time


def _keys(p: Project) -> set[str]:
    """Names a dependency can use to point at this project."""
    keys = {p.status.name, p.id, p.repo, p.repo.split("/")[-1]}
    if "/" in p.path:
        keys.add(p.path.rsplit("/", 2)[-2])  # folder: projects/<folder>/STATUS.md
    return {k.strip().casefold() for k in keys if k}


def resolve_dependencies(projects: list[Project]) -> None:
    """Fill each project's blocked_by: dependencies that are missing or not done."""
    lookup: dict[str, Project] = {}
    for p in projects:
        for key in _keys(p):
            lookup.setdefault(key, p)
    for p in projects:
        blocked = []
        for dep in p.status.depends_on:
            target = lookup.get(dep.casefold())
            if target is None or target is p:
                blocked.append(dep)
            elif target.status.status != "done":
                blocked.append(target.status.name)
        p.blocked_by = blocked


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
        self._pulls: dict[str, tuple[float, list[dict]]] = {}
        self._pull_locks: dict[str, asyncio.Lock] = {}

    async def open_pulls(self, repo: str) -> list[dict]:
        if not self.settings.github_token:
            return []
        async with self._pull_locks.setdefault(repo, asyncio.Lock()):
            cached = self._pulls.get(repo)
            if cached and time.monotonic() - cached[0] < self.settings.cache_seconds:
                return cached[1]
            owner, name = repo.split("/", 1)
            variables = {"owner": owner, "name": name, "after": None}
            pulls = []
            try:
                async with self._client() as client:
                    while True:
                        response = await client.post("/graphql", json={"query": _OPEN_PULLS, "variables": variables})
                        response.raise_for_status()
                        payload = response.json()
                        if payload.get("errors"):
                            raise GitHubError("Pull request information unavailable")
                        page = payload["data"]["repository"]["pullRequests"]
                        for pr in page["nodes"]:
                            commits = pr["commits"]["nodes"]
                            rollup = commits[-1]["commit"]["statusCheckRollup"] if commits else None
                            state = (rollup or {}).get("state")
                            checks = {"SUCCESS": "passed", "FAILURE": "failed", "ERROR": "failed",
                                      "PENDING": "pending", "EXPECTED": "pending"}.get(state, "none")
                            pulls.append({"number": pr["number"], "title": pr["title"], "checks": checks})
                        if not page["pageInfo"]["hasNextPage"]:
                            break
                        variables["after"] = page["pageInfo"]["endCursor"]
            except httpx.HTTPStatusError as exc:
                # Cache permanent permission/not-found errors to avoid hammering GitHub,
                # but never cache transient 5xx / rate-limit failures for 5 minutes.
                if exc.response.status_code in (401, 403, 404):
                    self._pulls[repo] = (time.monotonic(), [])
                return []
            except (httpx.HTTPError, GitHubError, ValueError, KeyError, TypeError):
                return []
            self._pulls[repo] = (time.monotonic(), pulls)
            return pulls

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
        if r.is_server_error:
            r.raise_for_status()
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
                    status=status_mod.parse(
                        text, fallback_name=fallback, owner_tag=self.settings.owner_tag
                    ),
                )
            )
        return projects

    @staticmethod
    def _stamp_frontmatter(head: str, today: str, by: str = "human") -> str:
        eol = "\r\n" if "\r\n" in head else "\n"
        if re.search(r"(?m)^updated:", head):
            head = re.sub(r"(?m)^updated:[^\r\n]*", lambda _: f"updated: {today}", head, count=1)
        else:
            idx = head.rfind(f"{eol}---")
            if idx != -1:
                head = head[:idx] + f"{eol}updated: {today}" + head[idx:]
        if re.search(r"(?m)^updated_by:", head):
            head = re.sub(r"(?m)^updated_by:[^\r\n]*", lambda _: f"updated_by: {by}", head, count=1)
        else:
            head = re.sub(r"(?m)^(updated:[^\r\n]*)", lambda m: f"{m.group(1)}{eol}updated_by: {by}", head, count=1)
        return head

    async def complete_owner_action(self, project: Project, line: int, sha: str) -> None:
        status = project.status
        if sha != project.status_sha or not any(item.line == line for item in status.sections.owner_actions):
            raise ValueError("This action changed. Reload the project.")
        lines = status.body.splitlines(keepends=True)
        today = dt.date.today().isoformat()
        lines[line] = re.sub(r"\[ \]\s*", f"[x] {today} ", lines[line], count=1)
        head = self._stamp_frontmatter(status.source.removesuffix(status.body), today, "human")
        await self._write_status(project, head + "".join(lines), sha, "Complete owner action in STATUS.md")

    async def complete_item(self, project: Project, line: int, sha: str) -> None:
        """Tick an agent task in the Next list (already done, or no longer needed).

        Like an agent finishing it: the title gets today's date, `next` moves to
        the next unchecked task an agent can do, and `updated`/`updated_by` are
        refreshed so the web commit does not trigger a stale warning."""
        status = project.status
        items = [item for item in status.sections.upcoming if item.line == line]
        if sha != project.status_sha or not items:
            raise ValueError("This task changed. Reload the project.")
        lines = status.body.splitlines(keepends=True)
        today = dt.date.today().isoformat()
        lines[line] = re.sub(r"\[ \]\s*", f"[x] {today} ", lines[line], count=1)
        body = "".join(lines)
        remaining = [item.title for item in sections(body, owner_tag=status.owner_tag).upcoming]
        nxt = remaining[0] if remaining else ""
        if nxt and (": " in nxt or nxt[0] in "\"'[{&*!|>%@`#"):
            nxt = json.dumps(nxt, ensure_ascii=False)        # keep the YAML valid
        head = status.source.removesuffix(status.body)
        head = re.sub(r"(?m)^next:[^\r\n]*", lambda _: f"next: {nxt}".rstrip(), head, count=1)
        head = self._stamp_frontmatter(head, today, "human")
        await self._write_status(project, head + body, sha,
                                 f"Mark done in STATUS.md: {items[0].title[:60]}")

    async def _write_status(self, project: Project, source: str, sha: str, message: str) -> None:
        payload = {"branch": project.branch, "sha": sha, "message": message,
                   "content": base64.b64encode(source.encode("utf-8")).decode("ascii")}
        async with self._client() as client:
            response = await client.put(f"/repos/{project.repo}/contents/{project.path}", json=payload)
        if response.status_code == 409:
            self._cache = None
            raise ValueError("STATUS.md changed. Reload the project.")
        if response.status_code != 200:
            raise GitHubError(f"GitHub could not update STATUS.md ({response.status_code}). "
                              "Check Contents: write permission and branch protection.")
        self._cache = None

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

    # ------------------------------------------- pull requests (task thread)
    # These are synchronous: they run in a local task's own thread.
    def _sync_headers(self) -> dict[str, str]:
        if not self.settings.github_token:
            raise GitHubError("This needs a GitHub token.")
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "baton-project-hub",
            "Authorization": f"Bearer {self.settings.github_token}",
        }

    def open_pr(self, repo: str, branch: str, base: str, title: str, body: str,
                draft: bool = True) -> str:
        """Open a pull request, a draft if asked (and the plan allows it)."""
        headers = self._sync_headers()
        r = httpx.post(f"{API}/repos/{repo}/pulls", headers=headers, timeout=30,
                       json={"title": title, "head": branch, "base": base, "body": body,
                             "draft": draft})
        if draft and r.status_code == 422 and "draft" in r.text.lower():
            # Draft pull requests aren't available on private repos of free plans.
            r = httpx.post(f"{API}/repos/{repo}/pulls", headers=headers, timeout=30,
                           json={"title": title, "head": branch, "base": base, "body": body})
        if r.status_code != 201:
            raise GitHubError(f"GitHub did not open the pull request ({r.status_code}).")
        return r.json()["html_url"]

    def open_draft_pr(self, repo: str, branch: str, base: str, title: str, body: str) -> str:
        return self.open_pr(repo, branch, base, title, body, draft=True)

    def checks(self, repo: str, sha: str) -> dict[str, list[str]]:
        """Tests on a commit, by state: {"pending": [...], "passed": [...], "failed": [...]}.

        Reads GitHub Actions check runs and the older commit statuses. Skipped
        or neutral runs count as neither passed nor failed.
        """
        headers = self._sync_headers()
        out: dict[str, list[str]] = {"pending": [], "passed": [], "failed": []}
        r = httpx.get(f"{API}/repos/{repo}/commits/{sha}/check-runs", headers=headers,
                      params={"per_page": 100}, timeout=30)
        if r.status_code != 200:
            raise GitHubError(f"Could not read the tests of {sha[:7]} ({r.status_code}).")
        for run in r.json().get("check_runs", []):
            name = run.get("name") or "check"
            if run.get("status") != "completed":
                out["pending"].append(name)
            elif run.get("conclusion") == "success":
                out["passed"].append(name)
            elif run.get("conclusion") not in ("skipped", "neutral"):
                out["failed"].append(name)
        r = httpx.get(f"{API}/repos/{repo}/commits/{sha}/status", headers=headers, timeout=30)
        if r.status_code == 200:
            for st in r.json().get("statuses", []):
                state = {"pending": "pending", "success": "passed"}.get(st.get("state"), "failed")
                out[state].append(st.get("context") or "status")
        return out

    def comment(self, repo: str, number: int, body: str) -> None:
        r = httpx.post(f"{API}/repos/{repo}/issues/{number}/comments", headers=self._sync_headers(),
                       json={"body": body}, timeout=30)
        if r.status_code != 201:
            raise GitHubError(f"GitHub did not save the comment ({r.status_code}).")

    def merge(self, repo: str, number: int, sha: str) -> None:
        """Merge the pull request only if its head is still `sha` (nobody pushed since)."""
        r = httpx.put(f"{API}/repos/{repo}/pulls/{number}/merge", headers=self._sync_headers(),
                      json={"sha": sha, "merge_method": "merge"}, timeout=30)
        if r.status_code != 200:
            try:
                why = r.json().get("message", "")
            except ValueError:
                why = ""
            raise GitHubError(f"GitHub did not merge the pull request ({r.status_code}"
                              f"{': ' + why[:200] if why else ''}).")

    def delete_branch(self, repo: str, branch: str) -> bool:
        """Delete a merged `baton/*` remote branch (refuses any other branch name)."""
        if not branch.startswith("baton/"):
            return False
        r = httpx.delete(
            f"{API}/repos/{repo}/git/refs/heads/{quote(branch, safe='/')}",
            headers=self._sync_headers(),
            timeout=30,
        )
        return r.status_code in (204, 404, 422)

    def invalidate(self) -> None:
        """Forget the cached projects, e.g. after a task merged a STATUS.md change."""
        self._cache = None

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
                    sem = asyncio.Semaphore(8)

                    async def _fetch(repo_item: dict[str, Any]) -> list[Project]:
                        async with sem:
                            return await self._projects_in(client, repo_item)

                    results = await asyncio.gather(
                        *(_fetch(r) for r in repos), return_exceptions=True
                    )
                    for repo, res in zip(repos, results, strict=True):
                        if isinstance(res, Exception):
                            errors.append(f"{repo['full_name']}: {res}")
                            if self._cache:
                                projects.extend(p for p in self._cache[1] if p.repo == repo["full_name"])
                        else:
                            projects.extend(res)
            except (GitHubError, httpx.HTTPError) as exc:
                errors.append(str(exc) or exc.__class__.__name__)
                if self._cache:
                    projects = list(self._cache[1])
            order = {"high": 0, "medium": 1, "low": 2}
            projects.sort(key=lambda p: (order.get(p.status.priority, 1), p.status.name.lower()))
            resolve_dependencies(projects)
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
            nxt = s.next_task.replace("|", "\\|").replace("\n", " ")
            if p.blocked:
                nxt = ("🔒 Bloqueado hasta completar: " if es else "🔒 Blocked until done: ") \
                    + ", ".join(p.blocked_by) + ". " + nxt
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
