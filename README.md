# Barrelvault

Personal productivity tracker. The product draft is [SPEC.md](SPEC.md).

## Environment

| name                | required | purpose                                                                                |
| ------------------- | -------- | -------------------------------------------------------------------------------------- |
| `BARRELVAULT_DB`    | no       | SQLite path. Default `backend/barrelvault.db`. On the Pi, a path outside the checkout. |
| `BARRELVAULT_TOKEN` | yes      | Bearer token for `/api` and `/mcp`.                                                    |

## Database

Each connection turns foreign keys on, so a delete cascades. It uses WAL and a busy timeout, so a second user of the file waits while the first is writing.
