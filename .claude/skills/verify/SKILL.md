---
name: verify
description: Prove a change to pytest-adbc-replay works end to end. Use before saying a change is done, before committing, and before opening or updating a pull request.
---

# Verify a change

Run from the repository root:

```bash
export CI=true  # CI's skip gate: a skipped test fails the run
uv sync --locked --dev --group integration
uv tool install prek  # once; skip if prek is already on PATH
prek run --all-files
uv run pytest tests/unit/ > /tmp/unit.log 2>&1; echo "unit rc=$?"
uv run pytest tests/integration/ -v > /tmp/integration.log 2>&1; echo "integration rc=$?"
```

It passes when every command exits 0 and every printed `rc=` is 0. The integration suite
records against a real MySQL container and then replays, so it needs Docker and the
MySQL driver (`just install-dbc`, then `just install-foundry-drivers`, which runs `dbc
install mysql`). Without Docker, run the unit suite and say the integration suite did
not run.

`CI=true` turns on `tests/ci_skips.py`, which fails the run on any skip not allowed
there, as CI does. Without it a skipped test only shows in the summary, and a run that
skipped what you changed is not a pass.

Take each command's exit status from the command itself. In a pipeline `$?` is the last
command's status, so `cmd | tail` hides a failure. Redirect long output to a file, then
read the file.

## What this covers

- the plugin's record and replay paths, cassette keys and file round trips (unit suite)
- a real record-then-replay cycle against MySQL (integration suite)

## What it does not cover

- drivers other than MySQL (integration) and SQLite (unit): downstream repos (semolina,
  adbc-poolhouse) replay Snowflake and Databricks cassettes made with this plugin
- Python versions other than the one you ran: CI runs a version matrix

## When it fails

- Read the first failure and fix its cause. Do not rerun hoping for a green, and do not
  skip, loosen or delete the failing test to get one.
- If the failure is real but outside the change, say so in the PR and in your report
  rather than working around it.
