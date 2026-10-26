# Barrelvault

A task management system where a person and their agents collaborate. The product draft is [SPEC.md](SPEC.md).

## Environment

| name                         | required | purpose                                                                                                |
| ---------------------------- | -------- | ------------------------------------------------------------------------------------------------------ |
| `BARRELVAULT_DB`             | no       | SQLite path. Default `backend/barrelvault.db`. On the Pi, a path outside the checkout.                 |
| `BARRELVAULT_SESSION_LENGTH` | no       | Session lifetime in seconds. Default 604800 (7 days). `expires_at` is the mint time plus this value.   |
| `BARRELVAULT_KEY_LENGTH`     | no       | API key lifetime in seconds. Default 31536000 (365 days). `key_expires_at` is the mint time plus this value. |

## Database

Each connection turns foreign keys on, uses WAL, and sets a busy timeout of 5000 milliseconds so a second user of the file waits while the first is writing.
