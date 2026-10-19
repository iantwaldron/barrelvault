# Barrelvault v0.1 spec

Personal productivity tracker. User and optional agents share one store. The app works with no agent connected.

## Status of this draft

This document is a working draft. We are reviewing it section by section, and a section stays open until that pass accepts it. No application code and no database file yet.

## Stack and layout

Python dependencies are listed in [backend/requirements.txt](backend/requirements.txt).

| path        | role                      |
| ----------- | ------------------------- |
| `backend/`  | FastAPI application       |
| `frontend/` | React + Vite + TypeScript |

The frontend client is generated from the API's OpenAPI schema with `openapi-typescript`. v0.1 includes a web app manifest and icons so Safari can add the app to the Dock. `frontend/` is empty so far.

One FastAPI process serves the HTTP routes. MCP also runs over stdio for local development.

| route   | serves                 |
| ------- | ---------------------- |
| `/`     | built frontend         |
| `/api`  | REST                   |
| `/mcp`  | MCP, streamable HTTP   |
| `stdio` | MCP, local development |

## Database file

The file is `barrelvault.db`. It is created when the database code needs it. The path is in the [environment list](README.md#environment).

## Data model

The models are User, Task, Link, Block, and TaskHistory. Tags are a field on Task.

### User

| column   | type | notes                               |
| -------- | ---- | ----------------------------------- |
| id       | int  | pk                                  |
| name     | text | unique                              |
| is_agent | bool | false is a person; true is an agent |

One User row is seeded for the account, with `is_agent` false. An agent that writes has its own User row, with `is_agent` true. The flag is set when the row is created and stays put. User rows are kept. v0.1's single token acts as the seeded user. A write names another User when an agent is the one acting.

### Task

A task may contain tasks, with the same fields at every depth. An empty `parent_id` is a root, and a root may have no children. The default read is one level: roots, or the direct children of a parent. `tree` returns every task under that parent. Setting a parent checks the ancestor chain for a cycle.

| column           | type     | notes                                                     |
| ---------------- | -------- | --------------------------------------------------------- |
| id               | int      | pk                                                        |
| parent_id        | int      | FK to Task, nullable, empty for a root                    |
| title            | text     | required                                                  |
| notes            | text     | markdown, nullable                                        |
| tags             | text     | nullable; whitespace-separated tokens                     |
| status           | smallint | `0` todo, `1` in_progress, `2` completed; default `0`     |
| value_tier       | text     | `high`, `low`, `maintenance`; default `low`               |
| estimate_minutes | smallint | nullable                                                  |
| due_at           | datetime | UTC, nullable; a deadline, not a rank                     |
| source           | text     | `email`, `call`, `self`, `agent`, `other`; default `self` |
| created_by       | int      | FK to User, the creator                                   |
| created_at       | datetime | UTC                                                       |
| updated_at       | datetime | UTC                                                       |

`source` says where the task came from. `created_by` and `created_at` stay put for the life of the row. `updated_at` changes when the task row changes, and when a block or link on that task changes.

Completing a parent leaves its children open. A finished task stays until it is deleted. Deleting a task deletes its children, blocks, links, and history.

### Tags

Tags are a filter on the task, stored in `tags`. A tag is one token and contains no spaces. `deal:chad` is a tag. Tags carry no status and no URL.

On save, a helper takes the submitted string and returns the stored form: tokens separated by single spaces, trimmed, with empty tokens and duplicates removed. Order is kept. The tokens live on the task, so a later multi-user store has no shared tag catalog.

One helper takes a stored string and returns its tokens. The task screen and a history snapshot both display tags by passing their string to that helper. The list check uses those same tokens.

Changing `tags` is a change to the task row, so it bumps `updated_at` and writes a history snapshot.

### Link

| column  | type | notes                         |
| ------- | ---- | ----------------------------- |
| id      | int  | pk                            |
| task_id | int  | FK to Task, ON DELETE CASCADE |
| url     | text | required                      |

A repo, pull request, or any other URL is a link. The same URL may appear more than once. Linking between tasks is by id.

### Block

A block is a row on a task. The task is blocked while it has at least one block. Several blocks may be open, and each is released on its own. Releasing a block deletes that row. The task's status stays as it is.

`reason` is one line naming what the task is waiting on and who was asked. A due date can pass while a block is open.

| column     | type     | notes                         |
| ---------- | -------- | ----------------------------- |
| id         | int      | pk                            |
| task_id    | int      | FK to Task, ON DELETE CASCADE |
| reason     | text     | required                      |
| created_by | int      | FK to User                    |
| created_at | datetime | UTC                           |

### History

`HistoryBase` holds the columns every history table extends. `TaskHistory` adds `task_id` and is stored as `task_history`. In the row below, `id`, `user_id`, `created_at`, and `state` come from `HistoryBase`. `@history(TaskHistory)` on Task writes the rows. A later audited model extends `HistoryBase` and adds its own foreign key.

Block, Link, and User rows are themselves the record of being added or removed.

| column     | type     | notes                         |
| ---------- | -------- | ----------------------------- |
| id         | int      | pk                            |
| user_id    | int      | FK to User, the editor        |
| created_at | datetime | when this snapshot was saved  |
| state      | json     | mutable fields at that moment |
| task_id    | int      | FK to Task, ON DELETE CASCADE |

Index `(task_id, created_at)`.

For a task, `state` holds `parent_id`, `title`, `notes`, `tags`, `status`, `value_tier`, `estimate_minutes`, `due_at`, and `source`. `parent_id` in `state` is a plain number, so deleting some other task leaves old snapshots alone. The recorder leaves out `id`, `created_by`, `created_at`, and `updated_at`.

Create writes the first snapshot. An update writes another only when one of those fields changed. The snapshot is the state being saved. The previous row is the prior state, and a diff is those two documents. After a successful commit, the newest row matches the live task.

The history insert and the task write are one transaction. A rollback or a failed commit drops both, and the stored task stays as it was.

## Value tier

`value_tier` is user-set. A task with no tier is `low`. Today is the tasks that are due or in progress, plus overdue and blocked. There is no stored top-three, rank, or strategic flag.

A model may suggest a tier later. That path is unused, and no source for the tier is stored. A tier change is a history row.

## Lists

`list_tasks`, the task list, today, stale items, and the daily summary load one working set, then filter and order it in the service. The query loads the account's tasks. Once more than one user exists, it loads that user's tasks. Status, tier, due date, text, and tags are applied in Python on that set. Each check takes the field value and the requested value. The tag check uses the shared token helper on the task's stored string. A tag matches a whole token. `deal` does not match `deal:chad`. Order is `high`, then `low`, then `maintenance`, then due date, with undated tasks after dated ones.

`list_tasks` takes `parent` and `tree`. `tree` is a bool and defaults to false. False returns one level: roots when `parent` is empty, and the direct children of `parent` when it is set. True returns every task under `parent`, and every task when `parent` is empty. The parent row itself stays on `get_task`. Each returned row includes `parent_id`, and the caller nests the rows. The checks above apply to the rows that walk returned. A search of every level sets `tree`. On the REST route the query value is the string `0` or `1`, mapped to false and true. MCP passes the bool. Today, stale items, and the daily summary load the whole working set and take no `tree` argument.

`get_task` stays a lookup by id. When time entries exist, an hours report sums in the query.

## MCP tools

Hand-written tools over the same service functions as REST. Descriptions are written for agents. Responses stay compact.

The acting user is the User on the write. `get_task` returns the task, its tags, links, open blocks, and history.

Block tools, for an agent or for the user:

- `apply_block(task_id, reason)` when an outbound email, or any other request, leaves the task waiting on someone. `reason` names what was asked and who was asked. Status does not change.
- `release_block(block_id)` deletes that row. Status does not change.

`daily_summary` uses "blocked" for tasks that have an open block.

Task reads and writes that already fit this schema:

- `list_tasks(filters: status, tier, tags, due_before, text, parent, tree=false)`
- `get_task(id)`
- `stale_items(days=7)`: open tasks whose `updated_at` is older than N days, plus overdue
- `daily_summary(date=today PT)`: due, in progress, overdue, blocked
- `create_task(...)`, `update_task(id, ...)`, `set_status(id, status)`, `set_due(id, due_at)`

## REST API

The same service functions under `/api`. Report routes that need time or goals wait on those models.

## UI (v0.1)

- Today: due, overdue, in progress, blocked, quick add.
- Task list: roots, then direct children, one level per read. A filter search sets `tree`. Filters for status, tier, and tag.
- Task detail: notes, tags, links, open blocks, history, estimate.
- Reports wait on the time model. Manifest and Pi deploy stay in v0.1.

## Agent consumers

Coach and Software are optional clients. The core loop never calls them or waits on them.

- Coach calls `daily_summary` at its 7:30 AM PT check-in.
- Software reads `stale_items`.

## Milestones

1. Settled models, Alembic migration, shared service layer, `@history`, tests for cascade delete and history.
2. MCP server at `/mcp` and auth middleware. Verify with MCP Inspector and over stdio.
3. React UI: Today, list, task detail.
4. Reports once time is specified, web manifest, systemd deploy to the Pi.
5. Optional: per-agent tokens and scopes; public demo page on avenida.ai.

## Not redrawn yet

These stayed in the original sketch when tasks collapsed to one table. When time entries exist, deleting a task deletes the time logged on it.

- **Goal**: id, title, description, period (for example `2026-Q4`), status, created_at.
- **TimeEntry**: id, task_id, category, started_at, ended_at, note, created_by. A running timer is an entry with no end. `actual_minutes` is the sum of entries. Categories are free-form strings.
- **Comment**: id, task_id, body, created_by, created_at. A comment bumps the task's `updated_at`.

Tools and screens that depend on them, and are not part of the settled list above: `time_report`, `weekly_goal_rollup`, `start_timer`, `stop_timer`, `log_time`, `add_comment`, hours-by-tier on the daily summary, and a weekly review of anything like tier overrides.

## Out of scope for v0.1

Multi-user, public hosting, notifications, recurring tasks, mobile app, desktop wrapper.

## Notes for the builder

- Keep MCP tools and REST routes as thin wrappers over one shared service layer.
- Table models use `table=True`. Response and report shapes are separate SQLModel classes.
- `HistoryBase` is not a table. `state` needs `sa_type=JSON`.
- The recorder reads the acting user from the session and writes history in that same transaction.
- Seed one User row for the account with `is_agent` false. Do not default `created_by` to a string.
- Do not create `barrelvault.db` until the database code needs it.
