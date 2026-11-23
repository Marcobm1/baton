# Auto-merge of local tasks (2026-11-23)

Marco asked for tasks sent from Baton to merge by themselves: Claude Code and
Codex work directly, OpenCode work only after Claude Code or Codex reviewed it.

## Rules

| Who did the work | Repository runs tests on GitHub | Before merging |
|---|---|---|
| Claude Code, Codex | yes | tests pass |
| Claude Code, Codex | no | the *other* one reviews (and fixes) |
| OpenCode, local AI | yes | review (and fixes), then tests pass again |
| OpenCode, local AI | no | review (and fixes) |

- **Tests.** Baton reads the GitHub check runs and commit statuses of the
  exact commit. If nothing appears within 2 minutes the repository is treated
  as having no tests, which means the work always gets a review instead.
  Pending tests are waited for up to 30 minutes.
- **Reviewer for OpenCode/local AI:** the one of Claude Code and Codex with
  more of its 5-hour window left (from the Quotas page data); a reviewer with
  0 % left is skipped. With no reading, Claude Code goes first.
- **Fixes.** Marco chose "the reviewer fixes it" over "leave it open": the
  reviewer edits the same worktree, Baton commits `Review fixes by …`, pushes,
  and the tests must pass again on the new commit. One review round only.
- **Verdict.** The review starts with `Verdict: APPROVE`, `FIXED` or `BLOCK`.
  `BLOCK`, no verdict, a crash or a timeout leave the PR open. The review is
  posted as a PR comment either way.
- **Safety.** The merge call passes the commit Baton checked (`sha`), so if
  anything was pushed to the branch meanwhile GitHub refuses the merge. Merge
  method: a merge commit, like the rest of the history. With auto-merge on,
  PRs are opened as ready (GitHub can't merge drafts).

## Alternatives not taken

- **GitHub's own auto-merge** needs branch protection with required checks,
  which these repositories don't have, and it can't run a review by an agent.
- **Review in GitHub Actions** (`@claude`) would spend Actions minutes and
  can't use Codex; the review runs locally like the task itself.

## Limits

- Cloud tasks (`@claude` in an issue) are not covered: the Claude GitHub
  Action opens its own PR.
- A task can't be cancelled once Baton is committing, reviewing or merging.
