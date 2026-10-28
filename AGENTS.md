# Barrelvault

[SPEC.md](SPEC.md) is the v0.1 product contract. Read it before planning or changing code, and follow it. Do not copy it into this file. If this file and the spec disagree about product behavior, the spec wins.

## Working rules

- Change only what the current task needs.
- Assume multi-tenancy. A single-tenant path hides a cross-account hole, so tests create a second tenant and refuse that access. SPEC.md defines the account boundary.
- Implement behavior once, in a shared service layer.
- Keep request state out of the service and model layer. A service call takes the acting user and every other input as arguments, and it does not read a session, a request, or the environment. The API and MCP authenticate and pass the acting user in, and settings load once at startup.
- Every function does real work for a real caller. Do not write one that always refuses, does nothing, or exists so a test has something to call. Enforce a rule where every write passes through it, and test it by attempting the write it forbids.
- A rule that holds for every write, whoever makes it, is a CHECK constraint or a trigger in the schema. A rule that depends on which call is writing goes in `guards.py`. Do not write a guarded table with a bulk statement or raw SQL outside the service call that owns that write.
- A service call flushes and never commits. Whoever opened the session owns the commit. Open sessions with `open_session()`, and pass `immediate=False` only for a session that never writes.
- Raise the `ServiceError` subclass that names the kind of refusal, with a message the caller can show. Login, the key exchange, and the session check raise one identical `Refused`.
- Regenerate revision `0001` until the first deploy. After that, every schema change gets a new revision, and a shipped revision is never edited.
- Use a stdlib virtualenv named `env` at `backend/env`. It is gitignored.
- Pin each direct Python dependency as `~=X.Y.Z` in the requirements files. The minor stays fixed, and patch releases stay allowed. Changing the minor means editing that line.
- Do not commit `.env` or `*.db`.
- When testing a model, insert nulls for nullable text columns with raw SQL, then read them back through the service. An ORM insert applies the default and hides the null the test is there to catch.
