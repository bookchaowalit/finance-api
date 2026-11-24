# Verify recipe (finance-api)

The `verify` skill follows this file. The checks are the ones CI runs (Python 3.12).

## Checks

```bash
uv sync --frozen --all-extras --python 3.12
uvx ruff@0.17.1 check . && uvx ruff@0.17.1 format --check .
.venv/bin/mypy
.venv/bin/pytest -q --cov --cov-report=
.venv/bin/coverage report --precision=2 --fail-under=92   # CI's gate: 91.75% fails
```

Cells are mocked with `httpx.MockTransport`; no database, no network.

## End to end

Default: exercise the change in process, as `tests/test_api.py` does: `create_app(TokenVerifier(<test
public PEM>, "empire-platform-test", "finance-api"), CellClient(<all 10 cells at https://<cell>.example>,
transport=httpx.MockTransport(...)))` driven by `TestClient` with a token signed by a throwaway RSA key.
For view changes, show one response with a failing cell: every section that depends on it carries the
error, the others are complete, status 200.

Real server (`/health` only, or partial-failure demos). `FINANCE_API_CELLS` is **merged** with the
production gateway defaults (`server.cells()`), so a partial override leaves the other cells on
production. Do not start the server unless all of the following hold:

```bash
# isolate everything that can reach a hosted service
unset FINANCE_API_BILLING_MODE FINANCE_API_CREDITS_URL FINANCE_API_IDENTITY_ISSUER \
  FINANCE_API_SERVICE_CLIENT_ID FINANCE_API_SERVICE_KEY_PEM FINANCE_API_SERVICE_WORKSPACE \
  FINANCE_API_EDGE_BYPASS FINANCE_API_ENTITLEMENT_SUBJECT FINANCE_API_JWT_JWKS_URL \
  FINANCE_API_JWT_IDENTITY_ISSUER FINANCE_API_JWT_IDENTITY_JWKS_URL SENTRY_DSN GATEWAY_ASSERTION_MODE
# all 10 cells, https (CellClient refuses http, even on localhost); .invalid never resolves
export FINANCE_API_CELLS="$(python3 -c 'import json; print(json.dumps({c: f"https://{c}.invalid" for c in
  "finance-accounts transactions budgets cash-flow reconciliations business-assets investments liabilities subscriptions tax".split()}))')"
export FINANCE_API_JWT_ISSUER=empire-platform-test   # never empire-platform-dev (refused) or a real issuer
export FINANCE_API_JWT_PUBLIC_KEY_PEM="$(cat <throwaway public key>.pem)"
# stop here unless no resolved cell URL points at production
.venv/bin/python -c 'import finance_api.server as s; u=s.cells().urls; assert len(u)==10 and not any("bookchaowalit.com" in v or "railway.app" in v for v in u.values()), u'
PORT=3790 HOST=127.0.0.1 .venv/bin/finance-api & pid=$!
until curl -fsS 127.0.0.1:3790/health; do sleep 0.5; done
kill $pid
```

Never send a production bearer to a local server. After a merge, check production `/health` reports
the new commit.

## Never

- Starting `finance-api` with any cell still on its production default, above all for erase/export.
- Setting `GATEWAY_ASSERTION_*` (gateway owner only).
