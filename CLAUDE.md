@AGENTS.md

## Claude Code

`AGENTS.md` above is the single source of truth — keep all shared rules there, not here.
This file exists only because Claude Code reads `CLAUDE.md`, not `AGENTS.md`.

- Use plan mode before changing anything under `backend/app/policy/` or
  `backend/app/tools/`. Those two directories are the authorization boundary; a wrong edit
  there is invisible until a judge exploits it live.
- `backend/app/domain/ports.py` is the contract four tracks build against. Changing a
  signature there is a cross-track event: `CHANGELOG.md` first, then the code.
