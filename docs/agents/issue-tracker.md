# Issue tracker

Issues, specifications, and PRDs live in GitHub Issues.

Repository: DengZhiyuan-math/Local-Ai-agent-for-latex

The current Git remote uses DengZhiyuan-math/prism-local, which GitHub resolves
to the same repository. Use the canonical name above for API and search operations.
Verified GitHub repository ID: R_kgDOUrfUxw.

## Operations

Prefer connected GitHub tools when they support the operation.
Otherwise use gh with the repository explicitly specified.

For multiline bodies with gh, use --body-file.

When a skill requests publication, create a GitHub issue.
Check for an existing matching issue before creating a duplicate.

Historical task-local Markdown records remain as history.
Local drafts are not a second authoritative tracker.

## Wayfinding operations

A map is a GitHub issue labelled `wayfinder:map`. Decision tickets are its
native sub-issues, with one `wayfinder:research`, `wayfinder:prototype`,
`wayfinder:grilling`, or `wayfinder:task` label. These labels describe the
workflow; keep triage labels consistent with docs/agents/triage-labels.md.

Use connected tools to create, read, assign, comment on, and update issues.
For native relationships that the connector does not expose, use `gh api`
with `repos/DengZhiyuan-math/Local-Ai-agent-for-latex` as the explicit prefix:

- List children: `GET /issues/{map_number}/sub_issues?per_page=100`.
- Add a child: `POST /issues/{map_number}/sub_issues` with integer
  `sub_issue_id`, the issue's database `id`, not its issue number or node ID.
  Check existing membership first. Do not replace another parent silently.
- List blockers: `GET /issues/{ticket_number}/dependencies/blocked_by?per_page=100`.
- Add a blocker: `POST /issues/{ticket_number}/dependencies/blocked_by` with
  integer `issue_id`, the blocking issue's database `id`.
- Claim: assign the ticket to the developer driving the map before working.
  An assignee is the claim; do not invent a second claim marker.
- Find the frontier: in native child order, select open, unassigned children
  for which every native blocker is closed. Follow pagination when present.
  A triage label alone does not establish that a ticket is unblocked.
- Resolve: post a resolution comment, close the ticket, then append its named
  link and a short gist to the map's Decisions so far. Read the current map
  before editing, so concurrent updates are preserved.

Create issues first, then wire dependencies. Keep engineering specifications
under their own specification issue; link them from decision tickets instead
of duplicating them. Use names as link text, never bare issue numbers.

API references: [sub-issues](https://docs.github.com/en/rest/issues/sub-issues)
and [issue dependencies](https://docs.github.com/en/rest/issues/issue-dependencies).

## Pull requests

PRs as a request surface: no.

External PRs do not enter the issue triage queue.
Normal PR review remains available when requested.

## Execution authority

Publishing a specification or applying ready-for-agent does not
override an explicit user instruction to pause implementation.
