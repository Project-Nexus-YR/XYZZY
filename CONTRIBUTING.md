# Contributing

## Setup

Python 3.11 or newer. From a clone:

```bash
python -m venv .venv
source .venv/bin/activate   # .venv\Scripts\Activate.ps1 on Windows
pip install -c constraints.txt -e ".[dev]"
```

`-c constraints.txt` pins the same dependency closure CI installs; without it
your local resolve can land on a version CI never tested.

## Before opening a pull request

Run the same checks the `gates` job in CI runs (`.github/workflows/ci.yml`):

```bash
pip install -c constraints.txt -e ".[e2e]" && python -m playwright install --with-deps chromium
python -m ruff check .
python -m ruff format --check .
python -m mypy src
python scripts/check_anchors.py
pip install pip-audit && pip-audit -r constraints.txt
python -m pytest
```

All six checks and the pytest run must pass. Without the Playwright install,
`tests/e2e/test_web_client.py` skips instead of failing, so a green local run
with no browser installed can still turn red in CI. `ruff format .` (no
`--check`) applies formatting fixes.

CI also has a `docker-build` job that builds the image and curls its health
endpoint; reproducing that locally needs Docker (`docker build -t xyzzy:ci .`
then run it and hit `/api/v1/health`), which this checklist does not require
before a PR.

## Commit and PR shape

One concern per commit, in the imperative mood (`fix: ...`, `feat: ...`,
`refactor: ...`, `docs: ...`), matching the existing history
(`git log --oneline`). Keep mechanical changes (formatting, generated files)
in their own commit, separate from behavior changes.

## Reporting a security issue

Do not open a public issue. See [SECURITY.md](SECURITY.md) for the private
reporting channel.

## License

Contributions are accepted under the project's [Apache 2.0 license](LICENSE).
