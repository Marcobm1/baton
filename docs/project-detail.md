# Project detail

The project page reads the project's own STATUS.md and overlays activity for
its GitHub repository. Projects in different folders of a hub share the
repository queue and PRs. Their checklists and progress stay independent.
There is no additional task or project database.

## Writing Next items

Use one top-level checkbox per task. Give it a short title. Indent optional
instructions and prerequisites below it, including how to verify completion.

```markdown
## Next
- [ ] Export the filtered report
  Include the visible columns in their current order.
  Use the sample report to check the header and row count.
- [x] 2026-11-23 Add report filters
```

The project progress bar counts only these checkboxes. The launch link opens
an editable draft with the repository, title and detail. It never queues work.
Keep drafts within the task form limits (200 characters for the title and
5,000 for the details).

## Reading the history

Done combines checked items with terminal Baton tasks, including failures
and cancellations labelled with their outcome. A cloud task with a recorded
agent summary also appears here. Each source keeps its own entry because a
task title is not a reliable identifier. Prefix a checked title with an ISO
completion date to place it chronologically. Entries with no recorded date
follow dated entries in source order. Ten entries are initially visible.

Write the newest Log entries first, as in the agent guide. The page shows the
first five and folds the remainder. The complete original STATUS.md, including
its front matter and any custom sections, stays at the bottom.

## GitHub reads

Opening a detail page requests the repository's open PRs and the combined check
state of each head commit in a paginated GraphQL query. Concurrent visitors
share the result in the Hub for the configured `BATON_CACHE_SECONDS`. This
optional query needs GitHub read access to pull requests and their checks.
Without a token or when the request fails, the page omits the PR list.
