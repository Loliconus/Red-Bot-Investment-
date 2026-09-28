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

Navigation is a fixed constructivist sidebar (Обзор `/`, Мысли бота `/reasoning`,
Рынок `/chart/{uid}`, Инструменты `/instruments`, Стратегия `/strategy`,
Риск `/risk`, Счёт и режим `/settings`, Журнал `/journal`, Пульт `/control`,
Хранилище `/admin/storage`, Бэктест `/backtest`). `/security` stays out of the
menu and opens from «Счёт и режим» and «Пульт». API paths did **not** move:
risk forms still post to `/risk/*`, sandbox/account admin to `/risk/account/*`,
control to `/control/*`, kill-challenge to `/security/kill/*`.

- P0 `/control`, `/security`, `/settings`, `/risk`: HTTP forms remain
  available when WS disconnects. Hard Stop is latched until full application
  restart and cannot be released via GUI (unlike soft pause). Restore requires
  a stopped scheduler, an exact typed confirmation and validated checksums;
  the GUI must then be restarted before trading. Broker API tokens are write-only
  in the GUI and the storage system must provide a usable keyring. Enabling
  counter-trend trading requires an explicit typed confirmation.
- P1 `/`, `/reasoning`: decisions are fetched from the repository and replayed
  after WS reconnect. «Мысли бота» adds the scan funnel, grouped rejection
  reasons (`decision_diagnostics.reasoning_overview`), per-instrument coverage
  and the last `DecisionCycleReport`; it refreshes live on the
  `reasoning.scan` channel. Resource levels distinguish unavailable from zero.
  MOEX phase is an **approximation**, not an exchange trading calendar.
- P1 `/strategy`: confluence threshold and module weights post to
  `/strategy/scoring` and are persisted as a new `strategy_configs` version via
  `update_strategy_config`. TA parameter editing is still disabled until
  versioned use cases are implemented. `/chart/{uid}` renders benchmark,
  Fibonacci, plan markers and live updates from available inputs.
- P2 `/instruments`, `/journal`: instrument enable/disable is persisted; approval
  of a hypothesis records a reviewed state **without** applying unspecified
  numeric strategy changes. The instrument catalog is loaded from
  `InstrumentsService` and persisted in DuckDB (`instrument_catalog`) — no
  ticker, lot size or UID is hardcoded; GUI search, backtest and the add-form
  read the saved catalog, and a refresh action re-fetches it from the API.
- P2 `/backtest`: launch fails closed with HTTP 501. Equity/trades are not
  fabricated from live account data. An isolated runner, walk-forward results,
  progress events and a working runner are **not complete** and are required
  before declaring the approved v1.0 done.

## Tests and build limitations

`pytest`, repository-wide `ruff check`/`ruff format --check` and strict `mypy`
pass locally on Python 3.11. GitHub CI also passes on Python 3.13 and 3.14
using separately installed public dependencies (T-Invest imports are lazy).
The project requires Python 3.14 for production. The Python 3.14 standalone
tarball and private T-Invest SDK registry could not be reached over TLS from
the development sandbox, so `uv.lock` has **not** been regenerated for the
newly declared GUI dependencies yet. Run `uv lock` with registry access before
merging; `uv sync --locked` will fail until then.

The Python integration tests exercise cookie/CSRF, append-only audit, Hard Stop,
WS sequence replay/gap, session-scoped SQL exports and real DuckDB backup/restore.
Browser E2E tests in `tests/e2e` exercise login, HTMX, WS and Hard Stop in a
real Chromium; run `playwright install chromium && pytest -m e2e`. They skip
if no browser is installed (Playwright's CDN was not reachable from this
sandbox), so browser verification remains an outstanding merge check. Never
use GUI controls to bypass a risk-triggered kill switch or show stored secrets.
