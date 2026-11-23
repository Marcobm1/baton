# Reviewing OpenCode delegations

Give the assignment a short type so you can recognise it later. Existing
commands without a type use `general`. For example, after choosing a listed
model, run:

```sh
baton delegate --list
baton delegate -m PROVIDER/MODEL --type test-draft "Draft test cases for parse()"
```

Baton prints the delegation ID on stderr and keeps the answer on stdout for
scripts. After checking the work, replace `123` below with that ID and choose
`codex` or `claude-code` to identify the reviewing agent:

```sh
baton rate 123 --agent codex --fulfillment 9 --correctness 8 --rework 7 --comment "Added the missing empty-input case."
```

Open **Ranking**, select the delegation, inspect its assignment and result,
and save your own scores. Either evaluator can go first. Repeating a review
updates that evaluator's entry. The comment is optional (500 characters).

| Criterion | What a 10 means |
|---|---|
| Assignment fulfillment | Covered the entire request and its constraints. |
| Correctness | The delivered result was correct when checked. |
| Rework | You could use the result exactly as delivered. |

All three scores are integers from 1 to 10. Each model's criterion averages
use every submitted review. The human and agent columns average the three
criteria for that evaluator across rated jobs. Models are sorted by human
score, then agent score. A missing review leaves a blank score. A job counts
once as evaluated when either review exists, including when both exist.

The CLI and web service share `ranking.sqlite3` in `BATON_DATA_DIR` (default
`~/.baton`). The history includes the exact model, start time, assignment
type and text, outcome and answer. Failed runs include the error, and timeouts
are marked. Keep this directory between restarts. With Docker, mount the
CLI's data directory as `BATON_HOST_DATA_DIR`.

For Baton-managed Claude Code and Codex tasks, the CLI first writes inside
the agent's worktree. Baton collects the records when the agent exits, before
it checks for changes to commit. The same delegation ID survives collection.
Only OpenCode subagents invoked via `baton delegate` enter this ranking.
