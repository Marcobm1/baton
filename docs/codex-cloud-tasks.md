# Codex cloud task triggers

Checked against OpenAI's documentation on 2026-11-24.

## What OpenAI documents

| Surface | Trigger | Result |
| --- | --- | --- |
| Codex GitHub integration | Post `@codex review` on an existing pull request, or enable automatic reviews and choose a review trigger in settings. | Codex posts a GitHub review of the PR diff. [GitHub integration guide](https://learn.chatgpt.com/docs/third-party/github) |
| Codex GitHub Security Review | Post `@codex security review` on a PR, or configure automatic security reviews where available. | Codex posts security findings on the PR. The full report is available in the associated task. [Security Review in GitHub](https://learn.chatgpt.com/docs/third-party/github#security-review) |
| Codex Cloud | Start a chat with **Work in → Cloud** and select an environment. | Codex runs the requested task remotely. You can inspect changes and open a PR from the result. [Codex Cloud guide](https://learn.chatgpt.com/docs/cloud) |
| Codex CLI | Use `codex cloud exec --env <ENV_ID> <QUERY>`. The CLI reference marks `codex cloud` experimental. | The signed-in CLI submits a remote task to the specified environment. [CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli#codex-cloud) |
| Codex GitHub Action | A repository workflow invokes `openai/codex-action` on a configured GitHub event. | Codex runs on a GitHub Actions runner and the documented setup uses an OpenAI API key. Baton's workflow rules currently exclude this action. [Codex GitHub Action guide](https://learn.chatgpt.com/docs/github-action) |

The current GitHub integration guide covers reviews. It gives no implementation
trigger for arbitrary `@codex` prompts in PR comments, GitHub issue titles or
bodies, issue comments, or issue assignment. Treat these as experiments for
GitHub. OpenAI says the existing GitHub integration still uses Codex Cloud
(Legacy) during the transition to the current cloud experience. [Using Codex Cloud](https://help.openai.com/en/articles/20001545-using-codex-cloud)

## How Baton currently uses GitHub issues

The cloud destination in **New task** sends `where=cloud` to `POST /tasks`.
Baton opens a GitHub issue with `@claude` and the prompt in its body. The
[Claude workflow](claude-task-workflow.yml) listens for an issue opening with
that mention. Baton saves the issue number and URL with status `sent` as soon
as GitHub accepts the issue.

Claude is asked to open a PR and post a report in the issue between
`BATON_SUMMARY_BEGIN` and `BATON_SUMMARY_END`. When the task page is opened,
Baton looks for those markers in comments by `claude[bot]` or
`github-actions[bot]` and saves the report. A Codex review lives on a PR that
already exists. A CLI submission starts with a cloud environment ID and would
need its own way to identify the run and retrieve its result. Replacing the
mention in the issue body would only establish that GitHub accepted the issue.

## Smoke test

Run this in a test repository with a disposable branch and a connected GitHub
account. Marco can set up access in Codex and use the intended Baton GitHub
identity for the API test. Keep credentials in their existing clients. Use a
small request with an observable diff, such as adding `codex-trigger-check`
to a test file on the disposable branch, and leave any resulting PR unmerged.

1. **Check the GitHub connection.** Enable Codex code review for the test
   repository and post `@codex review` in the conversation of an open test PR.
   Save the link to the resulting review. Diagnose repository access or review
   settings if the review fails. A successful review establishes that review
   is configured for that account and repository.
2. **Try the proposed GitHub trigger.** On an open test PR, post an
   implementation prompt such as `@codex Add codex-trigger-check to the test
   file on this branch and report the result here`. Check for a new cloud task,
   its final report, and the expected diff. For an issue-based proposal, try
   the same request in a fresh issue body and then in a comment on a separate
   test issue. Use a distinct marker each time to attribute the result to the
   right event. Record the surface, author, GitHub link, time and any reply or
   error. A reaction alone establishes acknowledgement.
3. **Repeat a successful GitHub trigger via the API.** Use a fresh test item
   and the same GitHub identity and token type planned for Baton. For a PR
   conversation comment, GitHub's issue-comment endpoint accepts the PR
   number. Check the actual author and confirm the API-created event launches
   a task and delivers the same kind of result as the manual event. HTTP 201
   confirms creation of the GitHub item. [GitHub comment API](https://docs.github.com/en/rest/issues/comments#create-an-issue-comment)
4. **Try direct cloud submission if evaluating the CLI route.** Use an
   environment for the test repository and a CLI already signed into Codex.
   Check `codex cloud exec --help` for the installed version. Version 0.171.0
   exposes `--branch` to select the test branch explicitly. Replace
   `TEST_ENV_ID` with the connected environment ID and `codex-trigger-test`
   with the test branch name. The quoted argument supplies the task prompt.

   ```text
   codex cloud exec --env TEST_ENV_ID --branch codex-trigger-test "Add codex-trigger-check to test.txt and report the result."
   codex cloud list --env TEST_ENV_ID --json
   ```

   The list command filters recent runs to that environment and prints JSON.
   Open the matching task in Codex and check the outcome and diff. Record what
   the CLI exposes for submission errors, task IDs, status and result links.

Before exposing a trigger in **New task**, record a run started through the
intended launch path, a stable task identifier or link, the expected diff on
the test branch or in a PR, and a result Baton can retrieve or link to. Record
how Baton can tell a rejected launch from a completed task and avoid duplicate
runs after an ambiguous submission. The application can then model the observed
result instead of treating GitHub issue creation as task completion.

Verified for this investigation: the official guides, Baton's issue and
report paths in source, and offline help for `codex cloud exec` and
`codex cloud list` in Codex CLI 0.171.0. The live smoke test is pending.
