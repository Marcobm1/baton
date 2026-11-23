# Task detail reports

The task page combines the stored task record with its local CLI log at read time.
Previously saved tasks use the same view without a migration. Baton steps describe
its own operations, such as creating a worktree or pushing a branch. Agent progress,
summaries and checks come from the agent output. New prompts ask for labelled lines.
Older replies can supply a final answer or a recognizable test result. When there
is no identifiable report, the page says so. A successful exit alone cannot
establish that any checks ran.

Claude failure advice uses the CLI response from an unsuccessful agent run.
The selected model name alone cannot establish account access or remaining quota.
A later Git or pull request failure keeps Baton's own failure message.

## Task summary (2026-11-23)

Two agent tasks (PRs #3 and #4) built the same "summary" panel in different
ways; they were combined into one:

- **Kept from #3:** the agent ends its run with a report between the lines
  `BATON_SUMMARY_BEGIN` and `BATON_SUMMARY_END`. The same rule works for every
  agent (Claude Code, Codex, OpenCode, local AI) without changing how their
  CLIs print, so the log stays readable and the cancel button keeps working.
  Cloud tasks ask Claude to post the same report on the issue, and the task
  page reads it from there.
- **Kept from #4:** the report also lists the problems met and what the owner
  needs to know or do, and the page never renders HTML from the agent's text.
- **Not kept from #4:** switching Claude Code to `--output-format text` and
  OpenCode to `--format json` to pick out their last reply. It depends on each
  CLI's output format and would have turned the OpenCode log into JSON.
- The report starts with `Summary:` and has a `Checks:` line, so the short
  lines at the top of the task page (`task_report.py`) still find them.
