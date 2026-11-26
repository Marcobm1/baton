"""Secure local task image artifacts (.baton-artifacts/) and structured report parsing (UX-3).

Security invariants:
  - Only raster images verified by BOTH extension and magic bytes (PNG, JPEG, WebP, GIF).
  - Never collects or serves SVG, HTML, scripts, symlinks, or hardlinked files (`st_nlink > 1`).
  - Always deletes `<worktree>/.baton-artifacts/` before `git status` / `git add`, so
    temporary screenshots never bloat git history or leak into pull requests.
  - Strictly validates task IDs and filenames against path traversal before serving.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any

ARTIFACTS_DIR_NAME = ".baton-artifacts"
MAX_ARTIFACTS = 8
MAX_ARTIFACT_BYTES = 5 * 1024 * 1024  # 5 MiB per image

ALLOWED_EXTS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

_SECTION_MAP = {
    "summary": "summary",
    "resumen": "summary",
    "changes": "changes",
    "cambios": "changes",
    "checks": "checks",
    "comprobaciones": "checks",
    "verification": "checks",
    "verificación": "checks",
    "next": "next",
    "siguiente": "next",
    "artifacts": "artifacts",
    "artefactos": "artifacts",
}
_SECTION_RE = re.compile(
    r"^\s*(?:#{1,4}\s*)?(?:\*\*)?"
    r"(summary|resumen|changes|cambios|checks|comprobaciones|verification|verificación|next|siguiente|artifacts|artefactos)"
    r"(?:\*\*)?\s*:\s*(.*)$",
    re.I,
)
_ARTIFACT_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?`?([^`|\n:]+\.(?:png|jpe?g|webp|gif))`?\s*(?:\||:|—|-)\s*(.+?)\s*$",
    re.I,
)


def detect_image_mime(header: bytes) -> str | None:
    """Identify safe raster image formats strictly from magic bytes."""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def safe_artifact_name(raw_name: str, expected_ext: str) -> str:
    base = Path(raw_name).name
    stem = Path(base).stem
    stem = _UNSAFE_CHARS.sub("_", stem).strip("._-")[:80] or "image"
    return f"{stem}{expected_ext}"


def parse_artifact_captions(summary: str) -> dict[str, str]:
    """Extract optional `filename.png | caption` descriptions from an agent summary."""
    captions: dict[str, str] = {}
    if not summary:
        return captions
    in_artifacts = False
    for raw in summary.splitlines():
        m_sec = _SECTION_RE.match(raw)
        if m_sec:
            sec = _SECTION_MAP[m_sec.group(1).lower()]
            in_artifacts = sec == "artifacts"
            inline = m_sec.group(2).strip()
            if in_artifacts and inline:
                m_art = _ARTIFACT_LINE_RE.match(inline)
                if m_art:
                    key = Path(m_art.group(1).strip()).name.lower()
                    captions[key] = m_art.group(2).strip()[:200]
            continue
        if in_artifacts:
            m_art = _ARTIFACT_LINE_RE.match(raw)
            if m_art:
                key = Path(m_art.group(1).strip()).name.lower()
                captions[key] = m_art.group(2).strip()[:200]
    return captions


def parse_structured_report(summary: str) -> dict[str, str] | None:
    """Parse structured agent summary sections (`summary`, `changes`, `checks`, `next`).

    Returns None when the summary does not follow the structured format so callers
    can render the raw Markdown cleanly without dropping any legacy text.
    """
    if not summary or not summary.strip():
        return None
    buckets: dict[str, list[str]] = {}
    current: str | None = None
    unmatched_before = 0
    for raw in summary.splitlines():
        m = _SECTION_RE.match(raw)
        if m:
            sec = _SECTION_MAP[m.group(1).lower()]
            current = sec
            buckets.setdefault(sec, [])
            rest = m.group(2).strip()
            if rest:
                buckets[sec].append(rest)
        elif current is not None:
            buckets[current].append(raw)
        elif raw.strip():
            unmatched_before += 1
    visible_keys = [k for k in ("summary", "changes", "checks", "next") if k in buckets]
    if len(visible_keys) < 2 or unmatched_before > 1:
        return None
    result = {k: "\n".join(buckets[k]).strip() for k in visible_keys}
    return {k: v for k, v in result.items() if v} or None


def collect_worktree_artifacts(
    wt: Path,
    data_dir: Path,
    task_id: str,
    summary: str = "",
) -> list[dict[str, Any]]:
    """Collect validated images from `<wt>/.baton-artifacts/` and delete the folder.

    Always removes `<wt>/.baton-artifacts` before returning so nothing is ever
    staged or committed to git.
    """
    src = Path(wt) / ARTIFACTS_DIR_NAME
    if not src.exists() and not src.is_symlink():
        return []
    if src.is_symlink():
        try:
            src.unlink()
        except OSError:
            pass
        return []
    if not src.is_dir():
        try:
            src.unlink()
        except OSError:
            pass
        return []

    if not _SAFE_ID.fullmatch(task_id or ""):
        shutil.rmtree(src, ignore_errors=True)
        return []

    captions = parse_artifact_captions(summary)
    dest_dir = Path(data_dir) / "artifacts" / task_id
    collected: list[dict[str, Any]] = []
    used_names: set[str] = set()

    try:
        entries = sorted(src.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        entries = []

    try:
        for entry in entries:
            if len(collected) >= MAX_ARTIFACTS:
                break
            try:
                st = os.lstat(entry)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if st.st_nlink > 1:
                # Refuse hardlinked files to prevent reading outside the worktree.
                continue
            if st.st_size <= 0 or st.st_size > MAX_ARTIFACT_BYTES:
                continue
            ext = entry.suffix.lower()
            expected_mime = ALLOWED_EXTS.get(ext)
            if not expected_mime:
                continue
            try:
                with entry.open("rb") as fh:
                    header = fh.read(16)
            except OSError:
                continue
            actual_mime = detect_image_mime(header)
            if actual_mime != expected_mime:
                continue

            name = safe_artifact_name(entry.name, ext)
            if name in used_names:
                stem = Path(name).stem
                idx = 2
                while f"{stem}_{idx}{ext}" in used_names:
                    idx += 1
                name = f"{stem}_{idx}{ext}"
            used_names.add(name)

            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / name
            try:
                shutil.copyfile(entry, target)
            except OSError:
                continue
            caption = captions.get(entry.name.lower()) or captions.get(name.lower()) or ""
            collected.append({
                "name": name,
                "mime": actual_mime,
                "size": st.st_size,
                "caption": caption,
            })
    finally:
        shutil.rmtree(src, ignore_errors=True)

    return collected


def resolve_artifact_path(data_dir: Path, task_id: str, filename: str) -> tuple[Path, str] | None:
    """Resolve an artifact file safely and re-verify its image magic bytes."""
    if not _SAFE_ID.fullmatch(task_id or ""):
        return None
    if not filename or filename != Path(filename).name or not _SAFE_ID.fullmatch(filename):
        return None
    ext = Path(filename).suffix.lower()
    expected_mime = ALLOWED_EXTS.get(ext)
    if not expected_mime:
        return None
    base = (Path(data_dir) / "artifacts" / task_id).resolve()
    target = (base / filename).resolve()
    if not target.is_relative_to(base):
        return None
    try:
        st = os.lstat(target)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size <= 0 or st.st_size > MAX_ARTIFACT_BYTES:
        return None
    try:
        with target.open("rb") as fh:
            actual_mime = detect_image_mime(fh.read(16))
    except OSError:
        return None
    if actual_mime != expected_mime:
        return None
    return target, actual_mime
