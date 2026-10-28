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

A response carries only what the call asked for: a task, one level, or a subtree, never the whole account by default. An agent pays for every row in its context. The API and MCP return `notes` and `dropped_reason` as text a person or agent wrote, and the MCP marks them as content, not instructions. The frontend renders `notes` as markdown with raw HTML off.

| route  | serves               |
| ------ | -------------------- |
| `/`    | built frontend       |
| `/api` | REST                 |
| `/mcp` | MCP, streamable HTTP |

## Database file

The file is `barrelvault.db`. It is created when the database code needs it. The path is in the [environment list](README.md#environment). `backend/barrelvault/settings.py` is the only reader of environment variables, and it applies the defaults. Settings load once when the process starts, and the caller passes the values the service needs, such as the session and key lengths.

## Data model

The models are User, Human, Agent, Token, Task, Block, and TaskHistory. Tags are a field on Task. Table models use `table=True`.

### User

| column | type | notes  |
| ------ | ---- | ------ |
| id     | int  | pk     |
| name   | text | unique within the account |

User is the actor. Human and Agent are profiles, one each to a User. The profile stores `user_id` as its primary key and as a foreign key to User. A user has one profile. The acting user is the user a call acts as. The API and MCP take it from the session they validated, and the service takes it as an argument.

The profile is the user's type, and the type is fixed. The service refuses a change that makes a human user an agent or an agent user a human. That includes replacing the profile and adding the other profile beside it. Retire and reassign do not convert the remaining user. Tests refuse each direction: replacing a Human profile with an Agent profile, replacing an Agent profile with a Human profile, adding an Agent profile to a human user, and adding a Human profile to an agent user.

The account is the human and every agent whose `owner_id` is that human's user id. Those agents are a model function on the human: the `Agent` rows with that `owner_id`, including retired agents. Task reads do not call it. A task belongs to the account when `owned_by` is that human's user id. `created_by` records who acted. The client does not send a user id. A response names a person or an agent, and it does not carry a user id.

### Human

| column        | type | notes                                 |
| ------------- | ---- | ------------------------------------- |
| user_id       | int  | pk, FK to User                        |
| email         | text | unique; normalized and lowercased     |
| password_hash | text | Argon2 hash of the person's password  |
| timezone      | text | IANA name, such as `America/Chicago`  |

A setup command creates the first human. The command is `python -m barrelvault.cli setup`. It applies the migrations, then checks the user table. Any row means setup has already run: the command prints a message and exits. Otherwise it prompts for the name, the email, the timezone, and the password twice. The timezone must be a name Python's `zoneinfo` knows. The email must be a valid address. Its syntax is checked with `email-validator`, without a network lookup, and it is stored in that library's normalized form, lowercased. Login normalizes the email it is given the same way before matching, so `Ada@Example.com` and `ada@example.com` are one login. A malformed email at login is the same failure as a wrong email. The two passwords must match. The password is read with echo off. It is not a command argument and it is not read from the environment. It is hashed with `pwdlib` and Argon2. The plaintext is not stored. The command writes one User and one Human. A second setup run is refused. In v0.1 setup is the only path that creates a Human, and no session can create one. A path that adds another human is out of scope for v0.1 and will be specified on its own. Tests that need a second account insert it directly.

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

Add creates one User and one Agent for an acting human. The name is required and unique within the account: it differs from the person's name and from every other agent the person has, compared without regard to case, so `Bot` and `bot` are one name. Another account may use the same name. `owner_id` is that human's user id. `key_hash`, `key_expires_at`, and `retired_at` start empty. Tests cover an add that stores those three empty, an add whose name is already taken in the account, in any case, and an add that reuses a name from another account.

`key_hash` is empty until the person issues a key. The raw key is shown once and is not stored. A model method on Agent generates it. The method reads at least 32 bytes from a cryptographic random source, stores the SHA-256 in `key_hash`, sets `key_expires_at` from the key length, and returns the raw key. The caller does not supply the secret. Issue runs only when `key_hash` is empty and `retired_at` is empty. It sets `key_hash` and `key_expires_at`. The service sets `key_expires_at` to the mint time plus the key length from settings. `BARRELVAULT_KEY_LENGTH` is that length in seconds. When it is unset or blank, the length is 31536000, which is 365 days. A value that is present and is not a positive integer is refused when settings load. Roll runs only when the agent has a key, live or expired, and `retired_at` is empty. An expired key is replaced the same way, so an agent is never left without a path to a new key. It replaces both, and the old key dies in that write. The same write deletes that agent's session row. One key is live. The caller does not send `key_expires_at`. The response that shows the raw key also shows `key_expires_at`. The exchange refuses a key whose `key_expires_at` is in the past. The service rejects a `key_hash` with an empty `key_expires_at`, a `key_expires_at` with an empty `key_hash`, and a change to `key_expires_at` outside issue, roll, and retire. Tests cover an issue that stores `key_expires_at` as the mint time plus the configured key length, a roll that replaces it, an unset length of 31536000, a length that is not a positive integer refused when settings load, an exchange of an expired key, a `key_hash` with an empty `key_expires_at`, a `key_expires_at` with an empty `key_hash`, an update that changes `key_expires_at` without a roll, an issue when a key is already live, an issue on a retired agent, a roll when `key_hash` is empty, and a roll on a retired agent.

Only an acting user with the Human profile can create or change an agent. That covers add, issue, roll, retire, reassign, and delete. An acting agent cannot write a User, a Human, or an Agent. Tests refuse an acting agent on each of those calls, and they refuse a Human written outside setup. Attached data is any task, block, or history row that points at the agent's user id. The session row does not count.

When nothing is attached, the delete removes the Agent profile, its session row, and the User row.

When something is attached, the call does nothing until the person chooses. The delete reports whether it removed the agent, so the caller knows to ask.

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

In v0.1 a read loads the account's tasks with one query, selecting the columns the tree needs and not `notes`, and a pure function builds the tree from those rows by grouping them on `parent_id`. The same function serves the one-level read, `tree`, the cycle check, and the dropped state below. A write that depends on the tree, such as the cycle check, loads it inside its own transaction. A later version may load a subtree with a recursive query instead, behind that same function.

| column           | type     | notes                                                     |
| ---------------- | -------- | --------------------------------------------------------- |
| id               | int      | pk                                                        |
| parent_id        | int      | FK to Task, nullable, ON DELETE CASCADE, empty for a root |
| title            | text     | required                                                  |
| notes            | text     | markdown, nullable; a URL is written here                 |
| tags             | text     | nullable; whitespace-separated tokens; save stores `""`   |
| status           | smallint | `0` todo, `1` in_progress, `2` completed, `3` dropped; default `0` |
| estimate_minutes | smallint | nullable                                                  |
| due_at           | datetime | UTC, nullable; a deadline, not a rank                     |
| created_by       | int      | FK to User, the creator                                   |
| created_at       | datetime | UTC, set by the system                                    |
| updated_at       | datetime | UTC                                                       |
| updated_by       | int      | FK to User, the last editor                               |
| owned_by         | int      | FK to User, the human who owns the task                   |

Every task and block read and write is limited to the acting user's account: `owned_by` must be the person's user id, or the acting agent's `owner_id`. A task in another account is treated as a missing id. Within the account, the person and every agent have the same control over tasks and blocks, deletes included. A task or block call names its acting user, and the service does not read a session. The API and MCP take the acting user from the session they validated. Another surface, such as a CLI, supplies it its own way. Tests create a second account and refuse a read, an update, a delete, a nest under its task, and a block add, edit, or release on its task.

The person or an agent can change `title`, `notes`, `tags`, `status`, `estimate_minutes`, `due_at`, and `parent_id`. Any status change is allowed, including a completed or dropped task back to todo, except on a blocked task, as the Block section says. `title` is required and is not blank after trimming. It is stored trimmed, with each run of whitespace inside it collapsed to one space. `status` is `0`, `1`, `2`, or `3`. `estimate_minutes` is from 0 through 32767.

An update changes only the fields the caller sends, and every other field keeps its stored value. Two fields also take an operation applied to the stored value inside the write: appending text to `notes`, and adding or removing tags. A caller that adds to a field uses the operation rather than reading the field and writing it back, so a change made between its read and its write is kept. An append to empty `notes` stores the text alone. An append to existing `notes` adds a blank line and then the text.

An update may carry the `updated_at` the caller read. When the stored `updated_at` differs, because the task or one of its blocks changed since, the update is refused as a conflict and nothing is written, so the caller reads again and decides on current values. An update without it is applied. An agent that reads, works, and then writes passes it. Tests cover a partial update that leaves the other fields alone, an append and a tag add that keep a change made after the caller's read, a stale `updated_at` refused with nothing written, a current one applied, and a block change that makes an earlier `updated_at` stale.

A create checks for a possible duplicate in the account. A task with a `due_at` matches a task of any status whose title matches without regard to case and whose stored `due_at` is the same UTC timestamp. A task with no `due_at` matches only an open task with no `due_at` and a matching title, so a completed or dropped undated task does not warn forever. A match refuses the create as a duplicate, a kind of conflict that creates nothing and carries the existing task's id, title, status, and `due_at`, so the caller can decide, and act on the existing task, without another read. The API and MCP return it with its own code. This is a warning, not a rule: a person can keep two tasks with one title. The check also makes a retried create safe, since the first attempt committed the task the retry then matches, and the write lock keeps two simultaneous creates from both passing it. Only a create is checked. An index on the account and the case-insensitive title serves the lookup.

A caller that wants the duplicate anyway confirms it by sending the create again with `allow_duplicate`, and the confirmation is honored only after a warning. The API and MCP share one surface module that records each warning it returns under a fingerprint of the acting user, the normalized title, and the stored UTC `due_at`, or none. A create with `allow_duplicate` and no recent warning for its fingerprint is refused, so an agent cannot skip the warning by sending the override first. A create with `allow_duplicate` and a recent warning is passed to the service with the check skipped, and that warning is used up, so one warning confirms one duplicate and a retried confirmation is refused rather than stored twice. The record is a dictionary of fingerprint to the time the warning was issued, guarded by a lock. Before each check it drops entries older than two minutes from the front, since a dictionary keeps insertion order, and it holds at most a fixed number of entries. An entry that has dropped off is simply absent, so an override after it is treated as one with no warning. It is in-memory state, so it lives in the surface layer, not the service, and the service takes only `allow_duplicate`. A restart forgets the warnings, so a confirmation sent after one is refused and the caller is warned again. Tests cover a match refused with the existing task's fields, for a title that differs only in case and spacing and for an existing task that is completed or dropped; an undated create matching an open undated task and not a completed one; a different `due_at` passed; the same title in another account passed; the confirmation in sequence, an override with no warning refused, then a warning, then the override stored, then a second override on that warning refused; an override from a different acting user refused; the record dropping entries past two minutes and past its size; and two simultaneous creates leaving one task, which holds the check and the insert to one transaction.

Both steps are cheap against a SQLite file in one process: the check is one more indexed read inside the create's own transaction, and the warning record is a dictionary lookup. When the store becomes a network service, the check costs a round trip, and the first remedy is to fold it into the insert as one conditional statement. When several app processes serve requests, the in-memory record no longer works, since each process holds its own. It then moves to a shared store with expiry, such as a Redis key set with `NX` and a two-minute timeout, or it is replaced by a signed confirmation token that the warning returns and the override echoes, which needs no server memory.

`owned_by` is set once at create and stays put. The person stores their own user id. An agent stores its `owner_id`. The service rejects a write whose `owned_by` is not the human's user id. Tests cover a create by the person, a create by an agent, an `owned_by` set to an agent id, an `owned_by` set to an id that is not the human, and an update that tries to change `owned_by`. `created_by` and `created_at` stay put for the life of the row. `created_by` is the acting user's id. Do not default it to a string. A Human profile on `created_by` means the person created the task. An Agent profile means an agent created it. `updated_at` and `updated_by` change when the task row changes, and when a block on that task changes. `updated_by` is the acting user. On create it is the same user as `created_by`, and `updated_at` equals `created_at`. Adding or releasing a block does not write a history snapshot.

How long a task has been open is a model function, not a stored column. For a completed or dropped task it is `updated_at` minus `created_at`. For any other status it is the current UTC time minus `created_at`. A later edit of a completed task moves `updated_at`, and the span moves with it. Block intervals are not subtracted.

Every nullable text column reads through `value or ""`. `None` becomes `""` before a caller or the client sees it. In v0.1 those columns are `notes` and `tags`. A test inserts a task with raw SQL, sets each of them to null, and reads them back through that path. Each read yields `""`. The insert stays outside the ORM.

Todo and in_progress are open. Completed and dropped are closed. Completing a parent leaves its children open. A finished task stays until it is deleted.

Dropped is for a task that stopped mattering without being done: superseded, a duplicate, or no longer wanted. Dropping a task changes only its own row. A task is effectively dropped when it or any ancestor is dropped, and the tree function works that out on read, so a descendant keeps its own status and comes back as it was when the ancestor is reopened or when it is moved out from under it. Completed does not pass down. A read of open work leaves out effectively dropped tasks. Tests cover a child under a dropped parent read as effectively dropped with its own status kept, the parent reopened and the child back, the child moved out and back, and a completed parent whose children stay open.

The drop call may take a reason. It sets `status` to `3` and appends the reason to `notes` in one write, as a fenced block labeled `dropped`:

````
```dropped
Superseded by the Q3 plan.
```
````

A read of a dropped task returns `dropped_reason`, the text of the last such block, and an open or completed task returns `""`. A pure function finds the block with plain string matching. It does not use a markdown library, and its output is plain text that is never evaluated or passed to a template. Reopening a task leaves the block in `notes` as the record. Tests cover a drop with a reason, a drop without one, two drops with the later reason returned, a reopened task returning `""`, and a block-shaped string in a reason that is returned as text.

Nesting and detaching change `parent_id`. Setting it to another task nests this task under that parent. A new task may be created already nested. The cycle check walks the ancestor chain and rejects the task itself, a descendant, a missing id, or a task in another account. Clearing `parent_id` detaches the task and makes it a root. Children stay under the moved task, and no rows are deleted.

Deleting a task removes that row. The `parent_id` cascade deletes the subtree with it, along with those tasks' blocks and history. There is no tombstone. Delete is for a mistake, such as a task created in error. Dropping is how a task stops mattering, and the MCP tool descriptions steer an agent to drop rather than delete.

### Tags

Tags are a filter on the task, stored in `tags`. A tag is one token and contains no spaces. `deal:chad` is a tag. Tags carry no status and no URL.

Tag reads, and the rest of tag CRUD, go through these helpers.

On save, a helper takes the submitted string and returns the stored form: tokens lowercased, separated by single spaces, trimmed, with empty tokens and duplicates removed. Order is kept, by first appearance. `Deal:Chad deal:chad` stores `deal:chad`. No tokens stores `""`. The tokens live on the task, so a later multi-user store has no shared tag catalog.

On read, the tag helper takes that coerced string and returns its tokens. `""` returns no tokens. The task screen and a history snapshot both display tags by passing their value to that helper.

A list can filter by tags. The filter's tags pass through the save helper, and a task matches when every one of them is a whole token in its `tags`. `deal` does not match `deal:chad`.

Adding a tag passes it through the save helper and puts it after the stored tokens, and adding one already there changes nothing. Removing a tag drops that token, compared in the stored lowercase form, and removing one that is not there changes nothing. An add or remove that leaves `tags` as it was is not a change: it does not bump `updated_at` or write a snapshot.

Changing `tags` is a change to the task row, so it bumps `updated_at` and writes a history snapshot.

A repo, pull request, or any other URL is written in the notes. There is no link row. A task points at another task through `parent_id`.

### Block

A block is a row on a task. The task is blocked while it has at least one open block. A block is open while `released_at` is empty. Several blocks may be open, and each is released on its own. Releasing a block sets `released_by` to the acting user and `released_at` to the current UTC time. A released block cannot be released again or reopened. The row stays. The task's status stays as it is.

A block is added only to an open task, todo or in_progress. While a task has an open block, its status does not change, in either direction. Closing a blocked task, by completing or dropping it, is refused as a conflict, unless the call asks to release the open blocks first: then it releases each one, as the acting user and at the current time, and changes the status in the same transaction. Triggers in the schema refuse a block added to a closed task and a status change on a task with an open block. An agent following up on open blocks leaves out effectively dropped tasks. Tests cover a block added to a completed and to a dropped task, refused, a status change on a blocked task, refused in each direction, a close that releases its blocks first, and a raw-SQL status change on a blocked task, refused by the schema.

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

For a task, `state` holds `parent_id`, `title`, `notes`, `tags`, `status`, `estimate_minutes`, and `due_at`. `due_at` is an ISO 8601 UTC string, `tags` is the stored form, and an empty `notes` is `""`. `parent_id` in `state` is a plain number, so deleting some other task leaves old snapshots alone. The recorder leaves out `id`, `created_by`, `created_at`, `updated_at`, `updated_by`, and `owned_by`. `created_at` on a history row is when the system saved that snapshot. `user_id` on that row is the editor of that snapshot. The recorder takes it from the task's `updated_by`, so it reads no session. History reads are ordered by `created_at`, then `id`.

Create writes the first snapshot. An update writes another only when one of those fields changed. The snapshot is the state being saved. The previous row is the prior state, and a diff is those two documents. After a successful commit, the newest row matches the live task.

The history insert and the task write are one transaction. A rollback or a failed commit drops both, and the stored task stays as it was.

## Out of scope for v0.1

Multi-user, public hosting, notifications, recurring tasks, mobile app, desktop wrapper. A `source` field for where a task came from, such as email or a call. Prioritization as a whole, including `value_tier`, rank, and a stored top-three. Goal and comments. Time entries, including timers and an hours report. Per-agent permission control. A person's setting to skip the duplicate confirmation. Assigning a task to a user, to be re-evaluated if agents start duplicating work; until then a `claimed:<name>` tag serves.
