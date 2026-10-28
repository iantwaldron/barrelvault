# Barrelvault v0.1 spec

A task management system where a person and their agents collaborate.

## Stack and layout

Python dependencies are listed in [backend/requirements.txt](backend/requirements.txt). Development dependencies are listed in [backend/requirements.dev.txt](backend/requirements.dev.txt).

| path        | role                      |
| ----------- | ------------------------- |
| `backend/`  | FastAPI application       |
| `frontend/` | React + Vite + TypeScript |

The frontend client is generated from the API's OpenAPI schema with `openapi-typescript`. v0.1 includes a web app manifest and icons so Safari can add the app to the Dock.

One FastAPI process serves the HTTP routes.

| route  | serves               |
| ------ | -------------------- |
| `/`    | built frontend       |
| `/api` | REST                 |
| `/mcp` | MCP, streamable HTTP |

## Database file

The file is `barrelvault.db`. It is created when the database code needs it. The path is in the [environment list](README.md#environment). `backend/barrelvault/settings.py` is the only reader of environment variables, and it applies the defaults.

## Data model

The models are User, Human, Agent, Token, Task, Block, and TaskHistory. Tags are a field on Task. Table models use `table=True`.

### User

| column | type | notes  |
| ------ | ---- | ------ |
| id     | int  | pk     |
| name   | text | unique |

User is the actor. Human and Agent are profiles, one each to a User. The profile stores `user_id` as its primary key and as a foreign key to User. A user has one profile. The acting user is the user a call acts as. The API and MCP take it from the session they validated, and the service takes it as an argument.

The profile is the user's type, and the type is fixed. The service refuses a change that makes a human user an agent or an agent user a human. That includes replacing the profile and adding the other profile beside it. Retire and reassign do not convert the remaining user. Tests refuse each direction: replacing a Human profile with an Agent profile, replacing an Agent profile with a Human profile, adding an Agent profile to a human user, and adding a Human profile to an agent user.

The account is the human and every agent whose `owner_id` is that human's user id. Those agents are a model function on the human: the `Agent` rows with that `owner_id`, including retired agents. Task reads do not call it. A task belongs to the account when `owned_by` is that human's user id. `created_by` records who acted. The client does not send a user id. A response names a person or an agent, and it does not carry a user id.

### Human

| column        | type | notes                                 |
| ------------- | ---- | ------------------------------------- |
| user_id       | int  | pk, FK to User                        |
| email         | text | unique; login matches this            |
| password_hash | text | Argon2 hash of the person's password  |
| timezone      | text | IANA name, such as `America/Chicago`  |

A setup command creates the first human. The command is `python -m barrelvault.cli setup`. It applies the migrations, then checks the user table. Any row means setup has already run: the command prints a message and exits. Otherwise it prompts for the name, the email, the timezone, and the password twice. The timezone must be a name Python's `zoneinfo` knows. The two passwords must match. The password is read with echo off. It is not a command argument and it is not read from the environment. It is hashed with `pwdlib` and Argon2. The plaintext is not stored. The command writes one User and one Human. A second setup run is refused. In v0.1 setup is the only path that creates a Human, and no session can create one. A path that adds another human is out of scope for v0.1 and will be specified on its own. Tests that need a second account insert it directly.

`timezone` is a profile attribute of the person, and only the person changes it. An agent works in its owner's timezone. Every datetime is stored in UTC. A datetime sent with an offset is converted to UTC. One sent without an offset is read in the account's timezone, and `zoneinfo` settles daylight-saving gaps and overlaps. A response carries UTC, and a read of the account returns the timezone so the client can show local time.

### Agent

| column         | type     | notes                                              |
| -------------- | -------- | -------------------------------------------------- |
| user_id        | int      | pk, FK to User                                     |
| owner_id       | int      | FK to User, the human this agent belongs to        |
| key_hash       | text     | nullable; SHA-256 of the API key                   |
| key_expires_at | datetime | UTC, nullable; set with the key; empty with no key |
| retired_at     | datetime | UTC, nullable; set when the agent is retired       |

`owner_id` is the human's user id. Many agents point at one human. The service rejects an `owner_id` that is not that human's user id. Tests cover an `owner_id` set to an agent id, an `owner_id` set to an id that is not the human, and an update that changes `owner_id`. An empty `retired_at` means the agent can act. A set `retired_at` means the person retired it. A retired agent stays in the account, its existing rows stay visible, and it cannot exchange a key or receive a new one.

Add creates one User and one Agent for an acting human. The name is required and unique. `owner_id` is that human's user id. `key_hash`, `key_expires_at`, and `retired_at` start empty. Tests cover an add that stores those three empty, and an add whose name is already taken.

`key_hash` is empty until the person issues a key. The raw key is shown once and is not stored. A model method on Agent generates it. The method reads at least 32 bytes from a cryptographic random source, stores the SHA-256 in `key_hash`, sets `key_expires_at` from the key length, and returns the raw key. The caller does not supply the secret. Issue runs only when `key_hash` is empty and `retired_at` is empty. It sets `key_hash` and `key_expires_at`. The service sets `key_expires_at` to the mint time plus the key length from settings. `BARRELVAULT_KEY_LENGTH` is that length in seconds. When it is unset or blank, the length is 31536000, which is 365 days. A value that is present and is not a positive integer is refused when settings load. Roll runs only when a key is live and `retired_at` is empty. It replaces both, and the old key dies in that write. The same write deletes that agent's session row. One key is live. The caller does not send `key_expires_at`. The response that shows the raw key also shows `key_expires_at`. The exchange refuses a key whose `key_expires_at` is in the past. The service rejects a `key_hash` with an empty `key_expires_at`, a `key_expires_at` with an empty `key_hash`, and a change to `key_expires_at` outside issue, roll, and retire. Tests cover an issue that stores `key_expires_at` as the mint time plus the configured key length, a roll that replaces it, an unset length of 31536000, a length that is not a positive integer refused when settings load, an exchange of an expired key, a `key_hash` with an empty `key_expires_at`, a `key_expires_at` with an empty `key_hash`, an update that changes `key_expires_at` without a roll, an issue when a key is already live, an issue on a retired agent, a roll when `key_hash` is empty, and a roll on a retired agent.

Only an acting user with the Human profile can create or change an agent. That covers add, issue, roll, retire, reassign, and delete. An acting agent cannot write a User, a Human, or an Agent. Tests refuse an acting agent on each of those calls, and they refuse a Human written outside setup. Attached data is any task, block, or history row that points at the agent's user id. The session row does not count.

When nothing is attached, the delete removes the Agent profile, its session row, and the User row.

When something is attached, the call does nothing until the person chooses.

- Reassign moves every task, block, and history column that points at the agent onto the person's user id, then removes the Agent profile, its session row, and the User row. Those rows then show the person's name. `owned_by` already points at the person and stays put.
- Retire sets `retired_at`, clears `key_hash` and `key_expires_at`, and deletes the agent's session row. The columns that point at the agent stay. The name still shows on what it did.

Each path runs in one transaction. A user row that remains has one profile.

### Token

A token row is a session. A user has at most one. The raw secret is shown once and is not stored. The row stores the SHA-256 hash.

| column      | type     | notes                                       |
| ----------- | -------- | ------------------------------------------- |
| id          | int      | pk                                          |
| user_id     | int      | unique; FK to User                          |
| secret_hash | text     | unique; SHA-256 of the secret               |
| created_at  | datetime | UTC, set by the system                      |
| expires_at  | datetime | UTC, set at mint; not nullable; not updated |

A session expires. The service sets `expires_at` to the mint time plus the session length from settings. `BARRELVAULT_SESSION_LENGTH` is that length in seconds. When it is unset or blank, the length is 604800, which is 7 days. A value that is present and is not a positive integer is refused when settings load. The caller does not send `expires_at`. The service rejects a session whose `expires_at` is missing, and it rejects an update that changes `expires_at`, `secret_hash`, or `user_id`.

Two paths mint a session, and nothing else does. Login matches `email` and `password_hash` on Human and writes that human's user id on the session. A wrong email and a wrong password are the same failure, and the password check runs in both cases. Neither mints a session. The agent exchange hashes the presented key, matches `Agent.key_hash`, and writes that agent's user id on the session. It refuses a retired agent, an empty `key_hash`, and an expired key. A session secret is not accepted on either path. A successful mint deletes that user's existing session row, if one exists, and inserts the new row in the same transaction. A model method on Token generates the secret from at least 32 bytes of cryptographic randomness. The insert stores that new secret. It does not update the old row. The caller does not supply the secret. After the transaction commits, that user has one session row. A refused mint deletes nothing. If the insert does not commit, the previous row stays. A committed state never has two session rows for one user.

Rolling a key deletes that agent's session row in the same transaction. Retire does too. The person's session belongs to a different user and stays.

The person's browser receives that session in a cookie that is `HttpOnly` and `SameSite=Lax`. The cookie is `Secure` when the scheme is HTTPS. Logout deletes that user's session row and clears the cookie. It does not write a new session. An agent receives the session token from the exchange and presents it on later calls. An agent does not log in with a password. The session identifies the acting user. It does not carry the account, a list of agent ids, or which path granted it. A later call accepts that session and refuses the API key.

Every later call on `/api` and `/mcp` validates the presented session with one function, and with no other. The function takes only the presented secret. The acting user is the user on the one row whose `secret_hash` matches. The secret is accepted only when `expires_at` is in the future. No matching row is a refusal. A different user's session does not affect it. When the matched user is an agent whose key is missing or past `key_expires_at`, the function deletes that session and refuses. A person's session is not deleted for that reason.

Tests cover a login with the person's password, a wrong password, an exchange with the live key, an exchange after that key was rolled, an empty `key_hash`, a retired agent, and a session secret presented to either path. Login stores the human's user id. The exchange stores the agent's user id. Both mint paths store `expires_at` as the mint time plus the configured session length. An unset length uses 604800. A length that is not a positive integer is refused when settings load. A session write with no `expires_at` is refused, and an update that changes `expires_at`, `secret_hash`, or `user_id` is refused. The check is called with the presented secret alone. A secret that matches no row is refused. The first mint for a user, with no prior row, leaves one row. A second successful mint for the same user leaves one row. The earlier secret matches no row while that expiry is still in the future, and the new secret is accepted. A refused mint leaves the existing row, and its secret is still accepted. That includes a wrong password, a session secret presented to either path, an expired key, an empty `key_hash`, a retired agent, and a write with no `expires_at`. A mint whose insert does not commit leaves the previous row, and its secret is still accepted. Two overlapping mints for the same user leave one row, and only one of the two secrets is accepted. A raw insert of a second row for the same `user_id` is refused, and the first row stays. A second mint for another user leaves both rows, one each, and the first user's secret is still accepted. An expired session is refused and is still that user's only row. A later successful mint replaces it and leaves one row, and the expired secret matches nothing. A roll deletes the agent's session row and leaves the person's session row. The agent's previous secret matches nothing. A roll with no session row leaves the agent with none. The exchange after that roll creates one row. Retire deletes the agent's session row, and that secret matches nothing. A later call with an expired session is refused, and a later call with the API key is refused. A wrong email leaves no session, the same as a wrong password. Logout deletes the row and does not insert another. An agent session whose key is past `key_expires_at`, or whose key is missing, is deleted and refused. The person's session row stays.

### Task

A task may contain tasks, with the same fields at every depth. An empty `parent_id` is a root, and a root need not have children. The default read is one level: roots, or the direct children of a parent. `tree` returns every task under that parent. In v0.1 every list, `tree` included, is ordered by `created_at`, then `id`. A later priority or grouping order replaces it.

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

Every task and block read and write is limited to the acting user's account: `owned_by` must be the person's user id, or the acting agent's `owner_id`. A task in another account is treated as a missing id. Within the account, the person and every agent have the same control over tasks and blocks, deletes included. A task or block call names its acting user, and the service does not read a session. The API and MCP take the acting user from the session they validated. Another surface, such as a CLI, supplies it its own way. Tests create a second account and refuse a read, an update, a delete, a nest under its task, and a block add, edit, or release on its task.

The person or an agent can change `title`, `notes`, `tags`, `status`, `estimate_minutes`, `due_at`, and `parent_id`. Any status change is allowed, including a completed task back to todo. `title` is required and is not blank after trimming. `status` is `0`, `1`, or `2`. `estimate_minutes` is from 0 through 32767.

`owned_by` is set once at create and stays put. The person stores their own user id. An agent stores its `owner_id`. The service rejects a write whose `owned_by` is not the human's user id. Tests cover a create by the person, a create by an agent, an `owned_by` set to an agent id, an `owned_by` set to an id that is not the human, and an update that tries to change `owned_by`. `created_by` and `created_at` stay put for the life of the row. `created_by` is the acting user's id. Do not default it to a string. A Human profile on `created_by` means the person created the task. An Agent profile means an agent created it. `updated_at` and `updated_by` change when the task row changes, and when a block on that task changes. `updated_by` is the acting user. On create it is the same user as `created_by`, and `updated_at` equals `created_at`. Adding or releasing a block does not write a history snapshot.

How long a task has been open is a model function, not a stored column. For a completed task it is `updated_at` minus `created_at`. For any other status it is the current UTC time minus `created_at`. A later edit of a completed task moves `updated_at`, and the span moves with it. Block intervals are not subtracted.

Every nullable text column reads through `value or ""`. `None` becomes `""` before a caller or the client sees it. In v0.1 those columns are `notes` and `tags`. A test inserts a task with raw SQL, sets each of them to null, and reads them back through that path. Each read yields `""`. The insert stays outside the ORM.

Completing a parent leaves its children open. A finished task stays until it is deleted.

Nesting and detaching change `parent_id`. Setting it to another task nests this task under that parent. A new task may be created already nested. The cycle check walks the ancestor chain and rejects the task itself, a descendant, a missing id, or a task in another account. Clearing `parent_id` detaches the task and makes it a root. Children stay under the moved task, and no rows are deleted.

Deleting a task removes that row. The `parent_id` cascade deletes the subtree with it, along with those tasks' blocks and history. There is no tombstone.

### Tags

Tags are a filter on the task, stored in `tags`. A tag is one token and contains no spaces. `deal:chad` is a tag. Tags carry no status and no URL.

Tag reads, and the rest of tag CRUD, go through these helpers.

On save, a helper takes the submitted string and returns the stored form: tokens lowercased, separated by single spaces, trimmed, with empty tokens and duplicates removed. Order is kept, by first appearance. `Deal:Chad deal:chad` stores `deal:chad`. No tokens stores `""`. The tokens live on the task, so a later multi-user store has no shared tag catalog.

On read, the tag helper takes that coerced string and returns its tokens. `""` returns no tokens. The task screen and a history snapshot both display tags by passing their value to that helper.

A list can filter by tags. The filter's tags pass through the save helper, and a task matches when every one of them is a whole token in its `tags`. `deal` does not match `deal:chad`.

Changing `tags` is a change to the task row, so it bumps `updated_at` and writes a history snapshot.

A repo, pull request, or any other URL is written in the notes. There is no link row. A task points at another task through `parent_id`.

### Block

A block is a row on a task. The task is blocked while it has at least one open block. A block is open while `released_at` is empty. Several blocks may be open, and each is released on its own. Releasing a block sets `released_by` to the acting user and `released_at` to the current UTC time. A released block cannot be released again or reopened. The row stays. The task's status stays as it is.

`reason` is one line naming what the task is waiting on and who was asked. It is required, is not blank after trimming, and can be edited. A block is deleted only with its task. A due date can pass while a block is open. An open block is the record an agent uses to bump the user, or to draft a follow-up to the person being waited on.

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

`HistoryBase` holds the columns every history table extends. It is not a table. `state` uses `sa_type=JSON`. `TaskHistory` adds `task_id` and is stored as `task_history`. In the row below, `id`, `user_id`, `created_at`, and `state` come from `HistoryBase`. `@history(TaskHistory)` on Task writes the rows. A later audited model extends `HistoryBase` and adds its own foreign key.

A block row is the record of the wait and of its release. A history row keeps its editor id. Reassigning an agent moves that id to the person before the user row is deleted. Retiring an agent leaves the id in place.

| column     | type     | notes                         |
| ---------- | -------- | ----------------------------- |
| id         | int      | pk                            |
| user_id    | int      | FK to User, the editor        |
| created_at | datetime | when this snapshot was saved  |
| state      | json     | mutable fields at that moment |
| task_id    | int      | FK to Task, ON DELETE CASCADE |

Index `(task_id, created_at)`.

For a task, `state` holds `parent_id`, `title`, `notes`, `tags`, `status`, `estimate_minutes`, and `due_at`. `due_at` is an ISO 8601 UTC string, `tags` is the stored form, and an empty `notes` is `""`. `parent_id` in `state` is a plain number, so deleting some other task leaves old snapshots alone. The recorder leaves out `id`, `created_by`, `created_at`, `updated_at`, `updated_by`, and `owned_by`. `created_at` on a history row is when the system saved that snapshot. `user_id` on that row is the editor of that snapshot. History reads are ordered by `created_at`, then `id`.

Create writes the first snapshot. An update writes another only when one of those fields changed. The snapshot is the state being saved. The previous row is the prior state, and a diff is those two documents. After a successful commit, the newest row matches the live task.

The history insert and the task write are one transaction. A rollback or a failed commit drops both, and the stored task stays as it was.

## Out of scope for v0.1

Multi-user, public hosting, notifications, recurring tasks, mobile app, desktop wrapper. A `source` field for where a task came from, such as email or a call. Prioritization as a whole, including `value_tier`, rank, and a stored top-three. Goal and comments. Time entries, including timers and an hours report. Per-agent permission control.
