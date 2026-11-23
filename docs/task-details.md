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
