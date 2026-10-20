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

The models are User, Token, Task, Block, and TaskHistory. Tags are a field on Task.

### User

| column        | type | notes                                                    |
| ------------- | ---- | -------------------------------------------------------- |
| id            | int  | pk                                                       |
| name          | text | unique                                                   |
| is_agent      | bool | false is a person; true is an agent                     |
| password_hash | text | nullable; Argon2 hash of the person's password          |

A check requires a person to have a `password_hash` and an agent to have none. One User row is seeded for the account, with `is_agent` false. An agent has its own User row, with `is_agent` true. The flag is set when the row is created and stays put. User rows are kept. The acting user is the user on the session.

There is no owner column. The account set is the seeded user's id and the id of each of that user's agents. In v0.1 every agent user is that user's agent. A task belongs to the account when `created_by` is in that set.

The server caches that set for the session, keyed by the session token. The cache stays on the server. Creating an agent user adds that id to the account's session caches. Every CRUD call checks against the ids in the cache. The client does not send a user id. A response names a person or an agent, and it does not carry a user id.

### Token

A token row is a session or an agent credential. The raw secret is shown once and is not stored. The row stores the SHA-256 hash.

| column      | type     | notes                          |
| ----------- | -------- | ------------------------------ |
| id          | int      | pk                             |
| user_id     | int      | FK to User                     |
| secret_hash | text     | SHA-256 of the secret          |
| kind        | text     | `session` or `agent`           |
| created_at  | datetime | UTC                            |
| expires_at  | datetime | UTC, nullable                  |
| revoked_at  | datetime | UTC, nullable                  |

A session row expires. An agent token lasts until it is revoked. A revoked token does not exchange.

The person's login checks the password and mints a session. The browser receives that session in an `HttpOnly` cookie. An agent does not log in with a password.

### Task

A task may contain tasks, with the same fields at every depth. An empty `parent_id` is a root, and a root may have no children. The default read is one level: roots, or the direct children of a parent. `tree` returns every task under that parent.

| column           | type     | notes                                                     |
| ---------------- | -------- | --------------------------------------------------------- |
| id               | int      | pk                                                        |
| parent_id        | int      | FK to Task, nullable, ON DELETE CASCADE, empty for a root |
| title            | text     | required                                                  |
| notes            | text     | markdown, nullable; a URL is written here                 |
| tags             | text     | nullable; whitespace-separated tokens; save stores `""`   |
| status           | smallint | `0` todo, `1` in_progress, `2` completed; default `0`     |
| estimate_minutes | smallint | nullable                                                  |
| due_at           | datetime | UTC, nullable; a deadline, not a rank                     |
| created_by       | int      | FK to User, the creator                                   |
| created_at       | datetime | UTC, set by the system                                    |
| updated_at       | datetime | UTC                                                       |
| updated_by       | int      | FK to User, the last editor                               |

`created_by` and `created_at` stay put for the life of the row. `created_by` together with that user's `is_agent` says whether an agent or the person created the task. `updated_at` and `updated_by` change when the task row changes, and when a block on that task changes. `updated_by` is the acting user. On create it is the same user as `created_by`. Adding or releasing a block does not write a history snapshot.

How long a task has been open is a model function, not a stored column. For a completed task it is `updated_at` minus `created_at`. For any other status it is the current UTC time minus `created_at`. A later edit of a completed task moves `updated_at`, and the span moves with it. Block intervals are not subtracted.

Every nullable text column reads through `value or ""`. `None` becomes `""` before a caller or the client sees it. In v0.1 those columns are `notes` and `tags`. A test inserts a task with raw SQL, sets each of them to null, and reads them back through that path. Each read yields `""`. The insert stays outside the ORM.

Completing a parent leaves its children open. A finished task stays until it is deleted.

Nesting and detaching change `parent_id`. Setting it to another task nests this task under that parent. A new task may be created already nested. The cycle check walks the ancestor chain and rejects the task itself, a descendant, or a missing id. Clearing `parent_id` detaches the task and makes it a root. Children stay under the moved task, and no rows are deleted.

Deleting a task removes that row. The `parent_id` cascade deletes the subtree with it, along with those tasks' blocks and history. There is no tombstone.

### Tags

Tags are a filter on the task, stored in `tags`. A tag is one token and contains no spaces. `deal:chad` is a tag. Tags carry no status and no URL.

Tag reads, and the rest of tag CRUD, go through these helpers.

On save, a helper takes the submitted string and returns the stored form: tokens separated by single spaces, trimmed, with empty tokens and duplicates removed. Order is kept. No tokens stores `""`. The tokens live on the task, so a later multi-user store has no shared tag catalog.

On read, the tag helper takes that coerced string and returns its tokens. `""` returns no tokens. The task screen and a history snapshot both display tags by passing their value to that helper. The list check uses those same tokens.

Changing `tags` is a change to the task row, so it bumps `updated_at` and writes a history snapshot.

A repo, pull request, or any other URL is written in the notes. There is no link row. A task points at another task through `parent_id`.

### Block

A block is a row on a task. The task is blocked while it has at least one open block. A block is open while `released_at` is empty. Several blocks may be open, and each is released on its own. Releasing a block sets `released_at` and `released_by` to the acting user and the current UTC time. The row stays. The task's status stays as it is.

`reason` is one line naming what the task is waiting on and who was asked. A due date can pass while a block is open. An open block is the record an agent uses to bump the user, or to draft a follow-up to the person being waited on.

| column      | type     | notes                              |
| ----------- | -------- | ---------------------------------- |
| id          | int      | pk                                 |
| task_id     | int      | FK to Task, ON DELETE CASCADE      |
| reason      | text     | required                           |
| created_by  | int      | FK to User                         |
| created_at  | datetime | UTC                                |
| released_by | int      | FK to User, nullable, set on release |
| released_at | datetime | UTC, nullable, set on release      |

### History

`HistoryBase` holds the columns every history table extends. `TaskHistory` adds `task_id` and is stored as `task_history`. In the row below, `id`, `user_id`, `created_at`, and `state` come from `HistoryBase`. `@history(TaskHistory)` on Task writes the rows. A later audited model extends `HistoryBase` and adds its own foreign key.

A block row is the record of the wait and of its release. User rows are kept.

| column     | type     | notes                         |
| ---------- | -------- | ----------------------------- |
| id         | int      | pk                            |
| user_id    | int      | FK to User, the editor        |
| created_at | datetime | when this snapshot was saved  |
| state      | json     | mutable fields at that moment |
| task_id    | int      | FK to Task, ON DELETE CASCADE |

Index `(task_id, created_at)`.

For a task, `state` holds `parent_id`, `title`, `notes`, `tags`, `status`, `estimate_minutes`, and `due_at`. `parent_id` in `state` is a plain number, so deleting some other task leaves old snapshots alone. The recorder leaves out `id`, `created_by`, `created_at`, `updated_at`, and `updated_by`. `created_at` on a history row is when the system saved that snapshot. `user_id` on that row is the editor of that snapshot.

Create writes the first snapshot. An update writes another only when one of those fields changed. The snapshot is the state being saved. The previous row is the prior state, and a diff is those two documents. After a successful commit, the newest row matches the live task.

The history insert and the task write are one transaction. A rollback or a failed commit drops both, and the stored task stays as it was.

## Lists

`list_tasks`, the task list, today, stale items, and the daily summary load one working set, then filter and order it in the service. The query loads tasks whose `created_by` is in the account set. Status, due date, text, and tags are applied in Python on that set. Each check takes the field value and the requested value. The tag check uses the shared token helper on the task's stored string. A tag matches a whole token. `deal` does not match `deal:chad`. Order is due date, with undated tasks after dated ones.

`list_tasks` takes `parent` and `tree`. `tree` is a bool and defaults to false. False returns one level: roots when `parent` is empty, and the direct children of `parent` when it is set. True returns every task under `parent`, and every task when `parent` is empty. The parent row itself stays on `get_task`. Each returned row includes `parent_id`, and the caller nests the rows. The checks above apply to the rows that walk returned. A search of every level sets `tree`. On the REST route the query value is the string `0` or `1`, mapped to false and true. MCP passes the bool. Today, stale items, and the daily summary load the whole working set and take no `tree` argument.

`get_task` stays a lookup by id inside the account set.

## MCP tools

Hand-written tools over the same service functions as REST. Descriptions are written for agents. Responses stay compact.

The acting user is the user on the session. `get_task` returns the task, its tags, open blocks, and history.

Only the person's session can add an agent or issue a token.

- `add_agent(name)` creates an agent user. `password_hash` is null. The new id joins the account's session caches.
- `issue_agent_token(name)` stores an agent token for that user and returns the raw secret once.

An agent presents that token to MCP. The server hashes it, finds the agent, mints a session, caches the account set under the session token, and returns the session token. Later MCP calls present the session token. The agent token is accepted only for that exchange. Stdio and streamable HTTP use the same exchange.

Block tools, for an agent or for the user:

- `apply_block(task_id, reason)` when an outbound email, or any other request, leaves the task waiting on someone. `reason` names what was asked and who was asked. Status does not change.
- `release_block(block_id)` sets `released_at` and `released_by`. The row stays. Status does not change.

`daily_summary` uses "blocked" for tasks that have an open block.

Task reads and writes that already fit this schema:

- `list_tasks(filters: status, tags, due_before, text, parent, tree=false)`
- `get_task(id)`
- `stale_items(days=7)`: open tasks whose `updated_at` is older than N days, plus overdue
- `daily_summary(date=today PT)`: due, in progress, overdue, blocked
- `create_task(...)`, `update_task(id, ...)`, `set_status(id, status)`, `set_due(id, due_at)`. `update_task` sets `parent_id` to nest and clears it to detach. Children stay with the task.

## REST API

The same service functions under `/api`.

## UI (v0.1)

- Today: due, overdue, in progress, blocked, quick add.
- Task list: roots, then direct children, one level per read. A filter search sets `tree`. Filters for status and tag. Nest a task under another, or detach it to a root. Children move with it.
- Task detail: notes, tags, open blocks, history, estimate.
- A person or an agent is shown by name. The browser does not store user ids.
- Manifest and Pi deploy stay in v0.1.

## Agent consumers

Coach and Software are optional clients. The core loop never calls them or waits on them.

- Coach calls `daily_summary` at its 7:30 AM PT check-in.
- Software reads `stale_items`.

## Milestones

1. Settled models, Alembic migration, shared service layer, `@history`, tests for cascade delete, history, and a raw-SQL null read of nullable text columns.
2. MCP server at `/mcp`, login, and the agent-token exchange. Verify with MCP Inspector and over stdio.
3. React UI: Today, list, task detail.
4. Web manifest and systemd deploy to the Pi.
5. Optional: public demo page on avenida.ai.

## Not redrawn yet

These stayed in the original sketch when tasks collapsed to one table.

- **Comment**: id, task_id, body, created_by, created_at. A comment bumps the task's `updated_at`.

Tools and screens that depend on them, and are not part of the settled list above: `add_comment`.

## Out of scope for v0.1

Multi-user, public hosting, notifications, recurring tasks, mobile app, desktop wrapper. A `source` field for where a task came from, such as email or a call. Prioritization as a whole, including `value_tier`, rank, and a stored top-three. Goal, until that object has been reviewed for inclusion. Time entries, including timers and an hours report.

## Notes for the builder

- Keep MCP tools and REST routes as thin wrappers over one shared service layer.
- Table models use `table=True`. Response and report shapes are separate SQLModel classes.
- `HistoryBase` is not a table. `state` needs `sa_type=JSON`.
- The recorder reads the acting user from the session cache and writes history in that same transaction. The cache holds the account set, keyed by the session token, and CRUD checks those ids.
- Seed one User row for the account with `is_agent` false. When that row has no `password_hash`, hash `BARRELVAULT_PASSWORD` into it with `pwdlib` and Argon2. Do not store the plaintext. Do not default `created_by` to a string.
- `pwdlib[argon2]` hashes the person's password. Agent and session secrets are stored as SHA-256. The user check is a database constraint, tested from both directions.
- Do not create `barrelvault.db` until the database code needs it.
