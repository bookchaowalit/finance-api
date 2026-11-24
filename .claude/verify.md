# Verify recipe (finance-api)

The `verify` skill follows this file. The checks are the ones CI runs.

## Checks

```bash
uv sync --frozen --all-extras
uvx ruff@0.17.1 check . && uvx ruff@0.17.1 format --check .
.venv/bin/mypy
.venv/bin/pytest -q --cov --cov-fail-under=92
```

Cells are mocked with `httpx.MockTransport`; no database, no network.

## End to end

Run `finance-api` locally with `FINANCE_API_CELLS` pointing at local or mocked cells (the default is
the production gateway: override it), then `curl -fsS localhost:3790/health`. For view changes, show
one response with a failing cell (section error, still 200). After a merge, check production
`/health` reports the new commit.

## Never

- Leaving `FINANCE_API_CELLS` at its production default for write paths (erase, export).
- Setting `GATEWAY_ASSERTION_*` (gateway owner only).
