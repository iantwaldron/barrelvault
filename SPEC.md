# Barrelvault v0.1 spec

Personal productivity tracker. User and optional agents share one store. The app works with no agent connected.

## Stack and layout

Python dependencies are listed in [backend/requirements.txt](backend/requirements.txt).

| path        | role                      |
| ----------- | ------------------------- |
| `backend/`  | FastAPI application       |
| `frontend/` | React + Vite + TypeScript |

The frontend client is generated from the API's OpenAPI schema with `openapi-typescript`. v0.1 includes a web app manifest and icons so Safari can add the app to the Dock.

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

The models are User, Human, Agent, Token, Task, Block, and TaskHistory. Tags are a field on Task.

### User

| column | type | notes  |
| ------ | ---- | ------ |
| id     | int  | pk     |
| name   | text | unique |

User is the actor. Human and Agent are profiles, one each to a User. The profile stores `user_id` as its primary key and as a foreign key to User. A user has one profile. The acting user is the user on the session.

The account is the human and every agent whose `owner_id` is that human's user id. Those agents are a model function on the human: the `Agent` rows with that `owner_id`, including retired agents. Task reads do not call it. A task belongs to the account when `owned_by` is that human's user id. `created_by` records who acted. The client does not send a user id. A response names a person or an agent, and it does not carry a user id.

### Human

| column        | type | notes                                 |
| ------------- | ---- | ------------------------------------- |
| user_id       | int  | pk, FK to User                        |
| email         | text | unique; login matches this            |
| password_hash | text | Argon2 hash of the person's password  |

An enrollment command creates the person. It prompts for the name, the email, and the password twice. The password is read with echo off and is not a command argument. It is hashed with `pwdlib` and Argon2. The plaintext is not stored. The command writes one User and one Human. A second human is refused.

### Agent

| column     | type     | notes                                        |
| ---------- | -------- | -------------------------------------------- |
| user_id    | int      | pk, FK to User                               |
| owner_id   | int      | FK to User, the human this agent belongs to  |
| retired_at | datetime | UTC, nullable; set when the agent is retired |

`owner_id` points at the human's user id. Many agents point at one human. An empty `retired_at` means the agent can act. A set `retired_at` means the person retired it. A retired agent stays in the account, its existing rows stay visible, and it cannot sign in or receive a new token.

Only the person's session can delete an agent. Attached data is any task, block, or history row that points at the agent's user id. Tokens do not count.

When nothing is attached, the delete removes the Agent profile, its tokens, and the User row.

When something is attached, the call does nothing until the person chooses.

- Reassign moves every task, block, and history column that points at the agent onto the person's user id, then removes the Agent profile, its tokens, and the User row. Those rows then show the person's name. `owned_by` already points at the person and stays put.
- Retire sets `retired_at` and revokes the agent's tokens. The columns that point at the agent stay. The name still shows on what it did.

Each path runs in one transaction. A user row that remains has one profile.

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

The person's login matches `email` and `password_hash` on Human and mints a session. The browser receives that session in an `HttpOnly` cookie. An agent does not log in with a password. The session identifies the acting user. It does not carry the account or a list of agent ids.

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
| owned_by         | int      | FK to User, the human who owns the task                   |

`owned_by` is set once at create and stays put. The person stores their own user id. An agent stores its `owner_id`. `created_by` and `created_at` stay put for the life of the row. A Human profile on `created_by` means the person created the task. An Agent profile means an agent created it. `updated_at` and `updated_by` change when the task row changes, and when a block on that task changes. `updated_by` is the acting user. On create it is the same user as `created_by`. Adding or releasing a block does not write a history snapshot.

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

A block row is the record of the wait and of its release. A history row keeps its editor id. Reassigning an agent moves that id to the person before the user row is deleted. Retiring an agent leaves the id in place.

| column     | type     | notes                         |
| ---------- | -------- | ----------------------------- |
| id         | int      | pk                            |
| user_id    | int      | FK to User, the editor        |
| created_at | datetime | when this snapshot was saved  |
| state      | json     | mutable fields at that moment |
| task_id    | int      | FK to Task, ON DELETE CASCADE |

Index `(task_id, created_at)`.

For a task, `state` holds `parent_id`, `title`, `notes`, `tags`, `status`, `estimate_minutes`, and `due_at`. `parent_id` in `state` is a plain number, so deleting some other task leaves old snapshots alone. The recorder leaves out `id`, `created_by`, `created_at`, `updated_at`, `updated_by`, and `owned_by`. `created_at` on a history row is when the system saved that snapshot. `user_id` on that row is the editor of that snapshot.

Create writes the first snapshot. An update writes another only when one of those fields changed. The snapshot is the state being saved. The previous row is the prior state, and a diff is those two documents. After a successful commit, the newest row matches the live task.

The history insert and the task write are one transaction. A rollback or a failed commit drops both, and the stored task stays as it was.

## Lists

`list_tasks`, the task list, today, stale items, and the daily summary load one working set, then filter and order it in the service. The query loads tasks whose `owned_by` is the human's user id. Status, due date, text, and tags are applied in Python on that set. Each check takes the field value and the requested value. The tag check uses the shared token helper on the task's stored string. A tag matches a whole token. `deal` does not match `deal:chad`. Order is due date, with undated tasks after dated ones.

`list_tasks` takes `parent` and `tree`. `tree` is a bool and defaults to false. False returns one level: roots when `parent` is empty, and the direct children of `parent` when it is set. True returns every task under `parent`, and every task when `parent` is empty. The parent row itself stays on `get_task`. Each returned row includes `parent_id`, and the caller nests the rows. The checks above apply to the rows that walk returned. A search of every level sets `tree`. On the REST route the query value is the string `0` or `1`, mapped to false and true. MCP passes the bool. Today, stale items, and the daily summary load the whole working set and take no `tree` argument.

`get_task` stays a lookup by id whose `owned_by` is the human's user id.

## MCP tools

Hand-written tools over the same service functions as REST. Descriptions are written for agents. Responses stay compact.

The acting user is the user on the session. `get_task` returns the task, its tags, open blocks, and history.

Only the person's session can add an agent, issue a token, or delete an agent.

- `add_agent(name)` creates a User and an Agent. `owner_id` is the acting user's id.
- `issue_agent_token(name)` stores an agent token for that user and returns the raw secret once. It refuses a retired agent.
- `delete_agent(name, choice)` follows the Agent delete rules. `choice` is empty, `reassign`, or `retire`. When rows are attached and `choice` is empty, the call deletes nothing and reports that a choice is required.

An agent presents that token to MCP. The server hashes it, finds the Agent, refuses a retired agent, mints a session, and returns the session token. Later MCP calls present the session token. The agent token is accepted only for that exchange. Stdio and streamable HTTP use the same exchange.

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
- Deleting an agent with nothing attached removes it. Deleting one with attached rows asks the person to reassign those rows to themselves, or to retire the agent and keep its name on them. Reassign says that the agent's name will disappear from those rows.

## Agent consumers

Coach and Software are optional clients. The core loop never calls them or waits on them.

- Coach calls `daily_summary` at its 7:30 AM PT check-in.
- Software reads `stale_items`.

## Milestones

1. Models, Alembic migration, shared service layer, `@history`, tests for cascade delete, history, and a raw-SQL null read of nullable text columns.
2. MCP server at `/mcp`, login, and the agent-token exchange. Verify with MCP Inspector and over stdio.
3. React UI: Today, list, task detail.
4. Web manifest and systemd deploy to the Pi.
5. Optional: public demo page on avenida.ai.

## Out of scope for v0.1

Multi-user, public hosting, notifications, recurring tasks, mobile app, desktop wrapper. A `source` field for where a task came from, such as email or a call. Prioritization as a whole, including `value_tier`, rank, and a stored top-three. Goal and comments. Time entries, including timers and an hours report.

## Notes for the builder

- Keep MCP tools and REST routes as thin wrappers over one shared service layer.
- Table models use `table=True`. Response and report shapes are separate SQLModel classes.
- `HistoryBase` is not a table. `state` needs `sa_type=JSON`.
- The recorder reads the acting user from the session and writes history in that same transaction. CRUD checks `owned_by` against the human's user id. `created_by` records the actor.
- `python -m barrelvault.cli create-user` is the enrollment command. It is a separate entry from stdio MCP. It applies the migrations, prompts for the name, the email, and the password twice, and writes one User and one Human. It refuses when a Human already exists. Do not store the plaintext. Do not read the password from the environment or from a command argument. Do not default `created_by` to a string.
- `pwdlib[argon2]` hashes the person's password. Agent and session secrets are stored as SHA-256. A user has one profile. An agent delete is one transaction: remove an unused agent, reassign and then remove it, or retire it.
- Do not create `barrelvault.db` until the database code needs it.
