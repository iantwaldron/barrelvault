# Barrelvault

[SPEC.md](SPEC.md) is the v0.1 product contract. Read it before planning or changing code, and follow it. Do not copy it into this file. If this file and the spec disagree about product behavior, the spec wins.

## Working rules

- Change only what the current task needs.
- Assume multi-tenancy. A single-tenant path hides a cross-account hole, so tests create a second tenant and refuse that access. SPEC.md defines the account boundary.
- Implement behavior once, in a shared service layer.
- Use a stdlib virtualenv named `env` at `backend/env`. It is gitignored.
- Pin each direct Python dependency as `~=X.Y.Z` in the requirements files. The minor stays fixed, and patch releases stay allowed. Changing the minor means editing that line.
- Do not commit `.env` or `*.db`.
- When testing a model, insert nulls for nullable text columns with raw SQL, then read them back through the service. An ORM insert applies the default and hides the null the test is there to catch.
