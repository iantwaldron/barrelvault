# Barrelvault

[SPEC.md](SPEC.md) is the v0.1 product contract. Read it before planning or changing code, and follow it. Do not copy it into this file. If this file and the spec disagree about product behavior, the spec wins.

## Working rules

- Change only what the current task needs. Leave SPEC.md "Out of scope" items alone unless the spec is updated.
- Implement behavior once, in a shared service layer. MCP tools and `/api` routes stay thin wrappers over that layer.
- Python lives in `backend/`. Use a stdlib virtualenv named `env` at `backend/env`. It is gitignored.
- Do not commit `.env` or `*.db`.
- When testing a model, insert nulls for nullable text columns with raw SQL, then read them back through the service. An ORM insert applies the default and hides the null the test is there to catch.
