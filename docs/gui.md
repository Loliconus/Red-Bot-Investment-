# Web GUI · development status

> Draft implementation of the approved GUI v1.0 specification. The operator
> must review the remaining P2 work before this PR is merged as v1.0.

## Launch

The GUI starts with the existing CLI (`redbot run --mode sandbox` or `backtest`).
Bind port/host with CLI options or `REDBOT_WEB__PORT`. In LIVE, configure a
**non-default** GUI session secret in keyring/environment. The GUI cannot start
in LIVE with the development password. Browser login uses an HttpOnly,
SameSite=Strict cookie; changes require a CSRF header. CLI/API clients may use
`X-Red-Bot-Token` returned by `/api/auth/login`. Place remote instances behind
HTTPS (and an access-controlled reverse proxy); the GUI itself does not
provide TLS.

The application serves local assets only. Node.js is needed **only** to rebuild
them during development:

```sh
./scripts/build_gui_js.sh
./scripts/build_gui_css.sh
```

Commit the compiled `adapters/driving/web/static/` assets with changes; runtime
uses Python, FastAPI, Jinja2 and these files. Browser dependencies and versions
are in `adapters/driving/web/assets/package-lock.json`; licenses and chart
attribution are under `static/vendor/` and on `/chart/{uid}`.

## Screens and operational constraints

- P0 `/control`, `/risk`, `/admin/storage`, `/security`: HTTP forms remain
  available when WS disconnects. Hard Stop is latched until full application
  restart and cannot be released via GUI (unlike soft pause). Restore requires
  a stopped scheduler, an exact typed confirmation and validated checksums;
  the GUI must then be restarted before trading. Broker API tokens are write-only
  in the GUI and the storage system must provide a usable keyring.
- P1 `/`, `/chart/{uid}`: decisions are fetched from the repository and replayed
  after WS reconnect. Resource levels distinguish unavailable from zero.
  Benchmark series, Fibonacci and live chart updates reflect available inputs.
  MOEX phase is an **approximation**, not an exchange trading calendar.
- P2 `/instruments`, `/journal`: instrument enable/disable is persisted; approval
  of a hypothesis records a reviewed state **without** applying unspecified
  numeric strategy changes. TA parameter editing is disabled until versioned
  use cases are implemented.
- P2 `/backtest`: launch fails closed with HTTP 501. Equity/trades are not
  fabricated from live account data. An isolated runner, walk-forward results,
  progress events and browser E2E tests are **not complete** and are required
  before declaring the approved v1.0 done.

## Tests and build limitations

`pytest`, `ruff check` and `mypy` pass locally on Python 3.11 for the applicable
sources; the project requires Python 3.14 for production. The Python 3.14
standalone tarball and private T-Invest SDK registry could not be reached over
TLS from the development sandbox, so `uv.lock` has **not** been regenerated
for the newly declared GUI dependencies yet. Run `uv lock` with access to the
configured registry before merging; `uv sync --locked` will fail until then.

The pre-existing Python tests exercise the HTTP API; GUI contract and browser
E2E coverage still need to be expanded. Never use GUI controls to bypass a
risk-triggered kill switch or to display stored secrets.
