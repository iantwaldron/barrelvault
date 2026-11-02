# Barrelvault

[![CI](https://github.com/iantwaldron/barrelvault/actions/workflows/ci.yml/badge.svg)](https://github.com/iantwaldron/barrelvault/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/iantwaldron/barrelvault/graph/badge.svg?token=WERYW1KUJK)](https://codecov.io/gh/iantwaldron/barrelvault)

A task management system where a person and their agents collaborate. [SPEC.md](SPEC.md) is the product contract.

## Install

Python 3.12 or newer. From `backend/`:

```sh
python3 -m venv env
env/bin/pip install --require-hashes -r requirements.dev.lock
env/bin/pip install --no-deps -e .
env/bin/barrelvault setup
```

`barrelvault setup` and `python -m barrelvault.cli setup` are the same command. It applies the migrations, then prompts for the name, the email, the timezone, and the password twice. The password is read with echo off. It is not a command argument and it is not read from the environment. If a user already exists, setup prints a message and exits.

A deploy installs `requirements.lock` instead, which leaves out the development tools.

### Locks

The lock files pin every package to a version and its hashes. After editing a requirements file, regenerate its lock from `backend/`:

```sh
env/bin/pip-compile --generate-hashes --allow-unsafe --strip-extras \
  --output-file requirements.lock requirements.txt
env/bin/pip-compile --generate-hashes --allow-unsafe --strip-extras \
  --output-file requirements.dev.lock requirements.dev.txt
```

To bring an existing virtualenv in line with the lock, sync it. `pip install` only adds packages, so one installed by hand stays until a sync removes it:

```sh
env/bin/pip-sync requirements.dev.lock
env/bin/pip install --no-deps -e .
```

## Checks

From `backend/`, these are the checks CI runs:

```sh
env/bin/ruff check .
env/bin/ruff format --check .
env/bin/mypy
env/bin/coverage run -m pytest
env/bin/coverage combine
env/bin/coverage report --fail-under=100
```

## Environment

`backend/barrelvault/settings.py` is the only reader of these variables. A blank value uses the default.

| Name                         | Required | Purpose                                                                                            |
| ---------------------------- | -------- | -------------------------------------------------------------------------------------------------- |
| `BARRELVAULT_DB`             | no       | SQLite path. Default `backend/barrelvault.db`. On the Pi, a path outside the checkout.            |
| `BARRELVAULT_SESSION_LENGTH` | no       | Session lifetime in seconds. Default 604800 (7 days). `expires_at` is the mint time plus this.    |
| `BARRELVAULT_KEY_LENGTH`     | no       | API key lifetime in seconds. Default 31536000 (365 days). `key_expires_at` is the mint time plus this. |

## Database

The file is created when the database code needs it. Each connection turns foreign keys on, uses WAL, and sets a busy timeout of 5000 milliseconds, so a second user of the file waits while the first is writing. Every transaction starts with an explicit `BEGIN`, reads included. A session starts with `BEGIN IMMEDIATE` unless it only reads, so a second writer waits out the busy timeout instead of failing, and two setup runs cannot both find an empty user table.

## Migrations

The schema is managed with Alembic. The revisions are in `backend/barrelvault/migrations/versions`, and setup applies them. After a model change, run this from `backend/` with the virtualenv active, then review the generated file:

```sh
env/bin/alembic revision --autogenerate -m "..."
```

Autogenerate does not see triggers, so a change to `models.TRIGGERS` is written into the revision by hand. A test compares the migrated triggers and CHECK constraints with the models.
