# Ranking models across delegations and Baton tasks

The **Origin** filter applies to both the job list and the model averages.
Choose **All origins** to combine delegations and tasks under the same model
name, or select **Delegation** or **Baton task** to compare one workload.

## Baton tasks

Every finished local task gets one entry, including
failures, cancellations and runs with no changes. A cloud task sent to GitHub
qualifies once Baton has stored its completion summary. Open **Rate** on the task
to add your evaluation; saving returns you to the Ranking list with a one-time
confirmation (your origin filter and chart view are kept). The entry links back to the task and shows its model,
start date (creation date if it never started), assignment, outcome, whether
Baton merged it, and the review report when present.

Claude Code and Codex rate the author's delivery before making review fixes.
They write the usual verdict and report, then a separate block like this:

```text
BATON_RATING_BEGIN
{"fulfillment":9,"correctness":8,"rework":7,"comment":"Added the missing empty-input test."}
BATON_RATING_END
```

Each score must be an integer from 1 to 10 and the comment must contain
1–500 characters. A missing or invalid block leaves the individual agent criteria blank.
The verdict still governs the merge.

On startup Baton imports finished tasks from `tasks.json`. A unique task ID
links each task to its Ranking entry. The entry, link and any structured agent
rating are written in one SQLite transaction. Restarting reuses that link and
preserves human scores. Older free-text reviews appear as written. If a historical record lacks a model name, the page shows
**Model not recorded**. Only new reviews request the structured scores.

## Delegations

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
use every submitted review. The chart compares models on a ten-point scale.
**Agent score** and **Your score** average each evaluator's job scores.
For **Combined average**, Baton averages both scores on each jointly rated
job, then averages those pairs by model. For example, agent 7 and human 9
contribute 8. A second job with only an agent score affects the agent chart
alone. The model table is ordered by human score, then agent score.

### Historical delivery estimates

If the job has no scored agent review, Baton computes a provisional delivery
score from the recorded verdict or outcome each time the page loads.

| First available evidence | Score |
|---|---:|
| Reviewer verdict APPROVE / FIXED / BLOCK | 9 / 7 / 3 |
| Merged | 8 |
| Completed | 6 |
| Failed or timed out | 2 |
| Cancelled, no changes, or unfinished, without stronger evidence | Unscored |

The job detail gives the reason for an estimate. This rubric measures delivery
as recorded, so an infrastructure failure can lower it. It supplies no
individual criterion grades and never writes a review on a person's or
agent's behalf. An explicit agent review takes precedence. Each job with at
least one score counts once in the table.

The CLI and web service share `ranking.sqlite3` in `BATON_DATA_DIR` (default
`~/.baton`). The history includes the exact model, start time, assignment
type and text, outcome and answer. Failed runs include the error, and timeouts
are marked. Keep this directory between restarts. With Docker, mount the
CLI's data directory as `BATON_HOST_DATA_DIR`.

For Baton-managed Claude Code and Codex tasks, the CLI first writes inside
the agent's worktree. Baton collects the records when the agent exits, before
it checks for changes to commit. The same delegation ID survives collection.

## The Ranking page (2026-11-24)

- **Summary tiles:** jobs, how many you have rated, how many wait for your
  score (a shortcut to the "To rate" list) and the best model for the chosen view.
- **One leaderboard** instead of a chart plus a table: each model shows its
  agent, your and the combined average side by side, with readable names
  (`opencode-go/glm-5.3` → GLM 5.3 · OpenCode Go). The view buttons choose the
  order. Each row says how much the average rests on (reviewed vs estimated
  agent scores, your scores) and warns when there are fewer than three.
- **Click a model** to list only its jobs; click again to clear.
- **Jobs** come 8 per page, with "All" and "To rate" tabs. Estimated agent
  scores are marked with an asterisk.

Why: the chart and the table repeated the same numbers, and estimated scores
(8.0 for every merged task) looked as solid as reviewed ones. Showing the
counts next to each average makes that visible without hiding the estimates.
