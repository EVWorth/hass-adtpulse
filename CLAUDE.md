# CLAUDE.md

hass-adtpulse is the Home Assistant integration for ADT Pulse alarm systems,
installed through HACS. All the portal work happens in
[pyadtpulse](https://github.com/homeassistant-projects/pyadtpulse); this repo
turns its client into a config flow, an update coordinator and entities.
Portal-facing bugs (login, MFA, parsing) usually belong in pyadtpulse.

## Commands

```bash
uv sync --all-groups             # create .venv with the dev and test groups
uv run pytest -q                 # the test suite (CI: tests.yaml)
uv run ruff check                # lint (CI: validate.yaml)
uv run ruff format --check       # format check (CI: validate.yaml)
uv run pre-commit install        # once per clone (also installs the commit-msg hook)
uv run cz bump --dry-run --yes   # preview the next automatic release
```

`uv.lock` is gitignored, so `uv sync` resolves fresh each time, skipping
releases younger than 2 days (`exclude-newer` in `[tool.uv]`; pyadtpulse is
exempt). `pytest-homeassistant-custom-component` pins an exact
`homeassistant`, so keep its floor in the `test` group: without one, a new
Home Assistant release makes uv fall back to a plugin too old to run.

## Layout

- `custom_components/adtpulse/__init__.py`: entry setup and unload. It turns
  login errors into `ConfigEntryAuthFailed` (reauth) or `ConfigEntryNotReady`
  (retry).
- `config_flow.py`: the user, MFA and reauth steps. `fingerprint.py` makes the
  browser-shaped fingerprint ADT trusts a device by.
- `coordinator.py`: one long-running task that waits on
  `PyADTPulseAsync.async_wait_for_update()` and pushes updates to the entities.
- `alarm_control_panel.py`, `binary_sensor.py`, `sensor.py`: entities.
- `tests/`: pytest-homeassistant-custom-component tests. Sockets are blocked,
  so mock the pyadtpulse client (`tests/conftest.py` has `make_pulse()`).

## Things that bite

- **Nobody can test against a live ADT account in CI**, and the maintainer
  doesn't have one. Prove changes with tests that fail without the fix, and say
  in the PR what was run against a real account, if anything.
- **Reauth is only for credentials.** Start it only on
  `PulseAuthenticationError` or `PulseMFARequiredError`. Every reauth makes the
  user sign in again, often with a new verification code. A dropped session
  (`PulseNotLoggedInError`) or a temporary lock (`PulseAccountLockedError`) is
  handled by logging back in.
- **Only one session per account works.** A second login with the same
  credentials logs the first one out.
- **pyadtpulse has no upper bound here** (`pyadtpulse>=3.0.0` in
  `manifest.json` and `pyproject.toml`), so a new pyadtpulse major reaches
  users as soon as it's published. Keep both requirement lines in step.
- **Ruff is pinned in two places** that must match: `ruff==` in the `dev`
  group (CI's ruff-action reads it) and the ruff-pre-commit `rev` in
  `.pre-commit-config.yaml`.
- **Imports are sorted by length** (`length-sort = true`). Run
  `uv run ruff check --fix` rather than ordering them by hand.
- **HACS validation reads the default branch** through the GitHub API, so a
  fix to what it checks (the license, `hacs.json`) only passes once it's on
  `main`.

## Workflow

- Pre-commit blocks commits to `main`, so work on a branch and open a PR.
  Merge with a merge commit.
- Docstrings follow the Google convention (ruff `D` rules).
- **Every commit must be a conventional commit** (`feat:`, `fix:`, `docs:`,
  `ci:`, `build:`, `chore:`, `test:`, `refactor:`, `perf:`). Releases are
  automatic: on each push to `main`, `release.yaml` runs Commitizen, which
  turns `feat` into a minor release and `fix`/`perf`/`refactor` into a patch,
  bumps the version in `pyproject.toml` and `manifest.json`, writes
  `CHANGELOG.md`, tags `X.Y.Z` and creates the GitHub release HACS installs.
  So pick the type for what users get, and never edit the version or the
  changelog by hand. Validate rejects non-conventional commits in PRs.
- While the version is 0.x, a breaking change bumps the minor
  (`major_version_zero`).
