# Barrelvault

A task management system where a person and their agents collaborate. The product draft is [SPEC.md](SPEC.md).

## Install

From `backend/`, with the virtualenv at `backend/env`:

```sh
python3 -m venv env
env/bin/pip install --require-hashes -r requirements.dev.lock
env/bin/pip install --no-deps -e .
env/bin/barrelvault setup
```

A deploy installs `requirements.lock` instead, which leaves out the development tools. The lock files pin every package to a version and its hashes. After editing a requirements file, regenerate its lock:

```sh
env/bin/pip-compile --generate-hashes --allow-unsafe --strip-extras \
  --output-file requirements.lock requirements.txt
env/bin/pip-compile --generate-hashes --allow-unsafe --strip-extras \
  --output-file requirements.dev.lock requirements.dev.txt
```

`barrelvault setup` and `python -m barrelvault.cli setup` run the same command.

## Environment

| name                         | required | purpose                                                                                                |
| ---------------------------- | -------- | ------------------------------------------------------------------------------------------------------ |
| `BARRELVAULT_DB`             | no       | SQLite path. Default `backend/barrelvault.db`. On the Pi, a path outside the checkout.                 |
| `BARRELVAULT_SESSION_LENGTH` | no       | Session lifetime in seconds. Default 604800 (7 days). `expires_at` is the mint time plus this value.   |
| `BARRELVAULT_KEY_LENGTH`     | no       | API key lifetime in seconds. Default 31536000 (365 days). `key_expires_at` is the mint time plus this value. |

## Database

Each connection turns foreign keys on, uses WAL, and sets a busy timeout of 5000 milliseconds so a second user of the file waits while the first is writing. Every transaction starts with an explicit `BEGIN`, reads included. A session starts with `BEGIN IMMEDIATE` unless it only reads, so a second writer waits out the busy timeout instead of failing, and two setup runs cannot both find an empty user table.

## Migrations

The schema is managed with Alembic. The revisions are in `backend/barrelvault/migrations/versions`, and setup applies them. After a model change, run `alembic revision --autogenerate -m "..."` from `backend/` with the virtualenv active, then review the generated file.
