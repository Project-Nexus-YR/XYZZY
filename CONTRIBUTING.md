# Contributing

## Setup

Python 3.11 or newer. From a clone:

```bash
python -m venv .venv
source .venv/bin/activate   # .venv\Scripts\Activate.ps1 on Windows
pip install -e ".[dev]"
```

## Before opening a pull request

Run the same checks CI runs (`.github/workflows/ci.yml`):

```bash
python -m ruff check .
python -m ruff format --check .
python -m mypy src
python -m pytest tests/
```

All four must pass. `ruff format .` (no `--check`) applies formatting fixes.

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
