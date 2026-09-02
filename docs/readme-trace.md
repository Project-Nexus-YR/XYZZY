# README claim trace

One row per number in README.md, the command run this session, and its
output. Carried rows (image pixel sizes, GIF duration, badge status) were
re-verified this session; the commands that produced them are below.

| Claim (README.md) | Command | Output |
| --- | --- | --- |
| "44 typed repository classes" / "44 typed repos" (lines ~92, ~112) | `grep -n "^class .*Repo" src/multiplayer/db/repositories.py \| wc -l` | `44` |
| "979 tests (978 passing, 1 skipped without OPENAI_API_KEY)" (Current Status) | `.venv/Scripts/python.exe -m pytest tests/ -q` | exit code 0; 979 collected (counted from the `.`/`s` result characters across all report lines, summed with the `--collect-only -q` per-file counts, both totalling 979); 1 `SKIPPED [1] tests/integration/test_live_provider.py:31: live provider test needs OPENAI_API_KEY` |
| Hero screenshot sizes: dark/light 1200x830, @2x 2400x1660; mobile 720x648, @2x 1440x1296 | `python -c "from PIL import Image; import glob; [print(f, Image.open(f).size) for f in sorted(glob.glob('site/assets/*.png'))]"` | `screenshot-hero-dark.png (1200, 830)`, `screenshot-hero-dark@2x.png (2400, 1660)`, `screenshot-hero-light.png (1200, 830)`, `screenshot-hero-light@2x.png (2400, 1660)`, mobile variants (720, 648) and (1440, 1296) |
| demo.gif: 960x600, 7 frames | `python -c "from PIL import Image; im=Image.open('site/assets/demo.gif'); print(im.size, im.n_frames)"` | `(960, 600) 7` |
| CI runs ruff/mypy/pytest on 3.11, pinned actions | `cat .github/workflows/ci.yml` | job runs `ruff check`, `ruff format --check`, `mypy src`, `pytest`, `python-version: "3.11"`, `uses:` lines pinned to commit SHAs |
| Apache 2.0 license consistency | `head -2 LICENSE`; `grep license pyproject.toml` | LICENSE starts "Apache License, Version 2.0"; `pyproject.toml` license field is `Apache-2.0` |
| Badges resolve | `curl -sI <badge and repo URLs>` | HTTP 200 for both Actions badge URLs and the repo page |

Carried-forward note: this repo had no prior `docs/readme-trace.md`, so there
is no earlier-round trace to diff against; every row above was produced or
re-checked in this session.
