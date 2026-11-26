# Auto-merge of local tasks (2026-11-23, rules changed 2026-11-25)

Tasks sent from Baton can merge by themselves. Under the 2026-11-25 policy,
Claude Code, Codex and Gemini (Antigravity CLI) run
with **full permissions** and their work merges **without any review**; only
OpenCode and local-AI work is reviewed, always by Claude Code.

## Rules

| Who did the work | Permissions while working | Before merging |
|---|---|---|
| Claude Code | `--dangerously-skip-permissions` | tests pass (or the repository has none) |
| Codex | `--dangerously-bypass-approvals-and-sandbox` + `--search` | tests pass (or none) |
| Gemini (`agy`) | `--dangerously-skip-permissions` | tests pass (or none) |
| OpenCode | Baton's `baton` agent (deny everything, then allow) | Claude Code review (and fixes), then tests pass again |
| Local AI (`codex --oss`) | Codex sandbox `workspace-write` | Claude Code review (and fixes), then tests pass again |

- **Tests.** Baton reads the GitHub check runs and commit statuses of the
  exact commit. If nothing appears within 2 minutes the repository is treated
  as having no tests. Pending tests are waited for up to 30 minutes; failing
  tests always leave the PR open (tests are not a review, so they still count
  for Claude Code, Codex and Gemini).
- **No tests.** Before 2026-11-25 a Claude Code or Codex task in a repository
  without tests was reviewed by the *other* one. Now it merges directly: these
  three agents already run with full permissions, and the extra review cost
  quota and time on every such task.
- **Reviewer for OpenCode/local AI:** always Claude Code (before: whichever of
  Claude Code and Codex had more 5-hour quota left). If Claude Code isn't
  installed or its 5-hour window shows 0 % left, the PR stays open.
- **Reviewer model:** Opus at medium effort by default (`BATON_REVIEW_CLAUDE_MODEL`
  / `_EFFORT` change it; set them empty for the CLI's own default). A model or
  effort the CLI doesn't list is ignored (CLI default) rather than making the
  review fail and leaving the PR open. The model and effort are saved on the
  task and shown next to "Review by". The Codex reviewer settings were removed
  with Codex reviews.
- **The reviewer keeps limited permissions** (`--permission-mode auto`): it
  reads work by a less trusted model, which could hide instructions for it in
  the files (prompt injection), so it does not get full permissions.
- **Fixes.** The reviewer fixes issues directly rather than leaving the PR open:
  the reviewer edits the same worktree, Baton commits `Review fixes by …`,
  pushes, and the tests must pass again on the new commit. One review round only.
- **Verdict.** The review starts with `Verdict: APPROVE`, `FIXED` or `BLOCK`.
  `BLOCK`, no verdict, a crash or a timeout leave the PR open. The review is
  posted as a PR comment either way.
- **Safety.** The merge call passes the commit Baton checked (`sha`), so if
  anything was pushed to the branch meanwhile GitHub refuses the merge. Merge
  method: a merge commit, like the rest of the history. With auto-merge on,
  PRs are opened as ready (GitHub can't merge drafts).

## What full permissions mean (the accepted trade-off)

The agent still starts inside the task's own git worktree, and Baton still
commits, pushes and merges itself. But with full permissions nothing stops the
agent from running any command, reaching the network, or writing outside the
worktree (for example in your profile or another clone). Codex loses its OS
sandbox; Claude Code loses the auto-mode classifier; Gemini runs every tool
without asking (agy still blocks its own system command denylist). In exchange
tasks never stall on a permission and can install packages, run servers or
search the web. Send these agents only tasks you wrote; content from outside
(web pages, other people's repositories, untrusted data) belongs with OpenCode
or local AI, which keep their limits and get a review. Abliterated local models
never get full permissions.

## Alternatives not taken

- **GitHub's own auto-merge** needs branch protection with required checks,
  which these repositories don't have, and it can't run a review by an agent.
- **Review in GitHub Actions** (`@claude`) would spend Actions minutes; the
  review runs locally like the task itself.
- **Keep reviewing trusted agents in repositories without tests**: safer, but
  trades away speed and quota for agents that are already trusted.

## Limits

- Cloud tasks (`@claude` in an issue) are not covered: the Claude GitHub
  Action opens its own PR.
- A task can't be cancelled once Baton is committing, reviewing or merging.
