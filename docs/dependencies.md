# Project dependencies and subprojects

Decision, 2026-11-24.

**Need.** Some projects cannot start until another one is finished (for
example, optimizing a local model needs the local model running first), and
very large projects are easier to follow as several smaller ones.

**Format.** Two optional front matter fields in `STATUS.md`:

- `depends_on`: a list (or a comma-separated string) of projects that must be
  `done` first.
- `parent`: the name of the bigger project this one belongs to. It is only
  shown as a label; it does not block anything.

**How a dependency is matched.** Any of: the other project's `name`, its
project id, its repository (`owner/repo` or `repo`), or its hub folder
(`projects/<folder>/STATUS.md`). Matching ignores letter case, so agents and
people can write the name they remember.

**Why "blocked" is computed and not a new `status` value.** A status like
`blocked` would have to be changed by hand twice: when blocking and again when
the dependency finishes, and people forget the second step. Computing it from
`depends_on` every time Baton loads the projects means the red warning appears
and disappears on its own. `status` keeps meaning where the work itself is
(`planned`, `active`, ...), which is still true while waiting.

**Unknown names block.** If a dependency matches no project, the project stays
blocked and the name is listed. A typo is then visible instead of silently
unblocking work that should wait.

**Trade-off.** Dependencies are only checked between projects Baton can see.
A dependency in a repository the token cannot read shows as blocked.
