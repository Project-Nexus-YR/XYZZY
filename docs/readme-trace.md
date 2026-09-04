# README claim trace

One row per number in README.md, the command run this session, and its
output. Carried rows (image pixel sizes, GIF duration, badge status) were
re-verified this session; the commands that produced them are below.

| Claim (README.md) | Command | Output |
| --- | --- | --- |
| "44 typed repository classes" / "44 typed repos" (lines ~92, ~112) | `grep -cE "^class [A-Za-z]+Repo:" src/multiplayer/db/repositories.py` | `44` |
| "979 tests (978 passing, 1 skipped without OPENAI_API_KEY)" (Current Status) | `.venv/Scripts/python.exe -m pytest tests/ -q` | exit code 0; 979 collected (counted from the `.`/`s` result characters across all report lines, summed with the `--collect-only -q` per-file counts, both totalling 979); 1 `SKIPPED [1] tests/integration/test_live_provider.py:31: live provider test needs OPENAI_API_KEY` |
| Hero screenshot sizes: dark/light 1200x830, @2x 2400x1660; mobile 720x648, @2x 1440x1296 | `python -c "from PIL import Image; import glob; [print(f, Image.open(f).size) for f in sorted(glob.glob('site/assets/*.png'))]"` | `screenshot-hero-dark.png (1200, 830)`, `screenshot-hero-dark@2x.png (2400, 1660)`, `screenshot-hero-light.png (1200, 830)`, `screenshot-hero-light@2x.png (2400, 1660)`, mobile variants (720, 648) and (1440, 1296) |
| demo.gif: 960x600, 7 frames | `python -c "from PIL import Image; im=Image.open('site/assets/demo.gif'); print(im.size, im.n_frames)"` | `(960, 600) 7` |
| demo.gif scenes are captured by `scripts/capture_scenes.py` from the seeded demo (entry, channel, thread, branch, brief, evidence chain, Ask Meta) and assembled by `scripts/build_demo_gif.py` | `python scripts/capture_scenes.py && python scripts/build_demo_gif.py build/scenes/*.png` | run 2026-09-02: 7 PNGs written, `site/assets/demo.gif: 7 frames, 960x600` |
| CI runs ruff/mypy/pytest on 3.11, pinned actions | `cat .github/workflows/ci.yml` | job runs `ruff check`, `ruff format --check`, `mypy src`, `pytest`, `python-version: "3.11"`, `uses:` lines pinned to commit SHAs |
| Apache 2.0 license consistency | `head -2 LICENSE`; `grep license pyproject.toml` | LICENSE starts "Apache License, Version 2.0"; `pyproject.toml` license field is `Apache-2.0` |
| Badges resolve | `curl -sI <badge and repo URLs>` | HTTP 200 for both Actions badge URLs and the repo page |
| Try-it command (line 20) serves the demo | `docker pull ghcr.io/project-nexus-yr/xyzzy` then `docker run -d -p 8000:8000 -e XYZZY_DEMO=1 ghcr.io/project-nexus-yr/xyzzy`; `curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/` | `Status: Downloaded newer image for ghcr.io/project-nexus-yr/xyzzy:latest`; HTTP 200 after 3 s; container log `Uvicorn running on http://0.0.0.0:8000` and `"GET / HTTP/1.1" 200 OK`; page title `<title>XYZZY</title>` |
| GitHub About box matches line 3 | `gh repo edit --description "A team makes a hard technical decision with AI, and keeps the receipts."`; `gh repo view --json description,homepageUrl,repositoryTopics` | description `A team makes a hard technical decision with AI, and keeps the receipts.`; homepage `https://xyzzy.yasserameur-dev.workers.dev/`; 10 topics: ai, ai-agents, audit-log, collaboration, decision-making, fastapi, multi-agent, self-hosted, sqlite, websockets |

| Six pre-credential OIDC routes have no auth dependency (README:170, auth boundary sentence) | `grep -n "^@router\\.\\(get\\|post\\)(\"/auth" src/multiplayer/api/routes.py` then read each handler signature | `/auth/config`, `/auth/login`, `/auth/callback`, `/auth/refresh`, `/auth/backchannel-logout`, `/auth/frontchannel-logout` take no `CurrentUser` parameter; `/auth/logout`, `/auth/logout-everywhere`, `/auth/end-session` do |
| `XYZZY_MAX_ATTACHMENT_BYTES` default (README deployment table) | `grep -n "DEFAULT_MAX_ATTACHMENT_BYTES" src/multiplayer/api/routes.py` | `DEFAULT_MAX_ATTACHMENT_BYTES = 5_242_880` |
| `XYZZY_OIDC_PROVIDER_LABEL` default (docs/SSO.md) | `grep -n "XYZZY_OIDC_PROVIDER_LABEL" src/multiplayer/api/routes.py` | `label = os.environ.get("XYZZY_OIDC_PROVIDER_LABEL", "single sign-on")` |
| `GET /metrics` shipped in v0.3.0 (CHANGELOG) | `git log --oneline v0.2.0..v0.3.0 -- src/multiplayer/metrics.py` | `3e80c5a feat(ops): expose Prometheus metrics on an unauthenticated /metrics` |
| Seven commits since v0.4.0 (CHANGELOG Unreleased) | `git log v0.4.0..HEAD --oneline` | 7 lines: `217f240`, `7580bb5`, `ba64bd3`, `afc5c3d`, `f08280a`, `a9c4fa9`, `95a4952` |

Carried-forward note: this repo had no prior `docs/readme-trace.md`, so there
is no earlier-round trace to diff against; every row above was produced or
re-checked in this session.
