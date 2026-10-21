# AGENT.md

Guidance for coding agents working in this repository. See `README.md` for what the project does and how to run it.

## Layout

```
app/                 Scraper (service `app`, image built from ./Dockerfile with Poetry)
  main.py            Entry point: creates tables, then runs scrape_cycle() on a schedule
  scraper.py         Fetching (Shift_JIS), HTML parsing, upserts, image download
  models.py          SQLAlchemy models: Thread, Post
  database.py        Engine / SessionLocal, built from POSTGRES_* env vars
dashboard/           FastAPI dashboard (service `dashboard`, dashboard/Dockerfile, pip)
  main.py            Auth middleware, login, JSON API (/api/*, incl. thread and post browsers), image and mosaic routes
  config.py          START_DATE (DASHBOARD_START_DATE)
  tokenizer.py       Worker: Japanese tokenization -> post_tokens, and people named in posts (via people.py)
  people.py          Person-name extraction from UniDic tags -> person, post_person
  classifier.py      image classifier -> image_classifications
  mosaic.py          Worker: runs the classifier, builds thumbnails and mosaics in the cache dir
  politics.py        Worker + CLI: political classification via TypeSafe AI -> post_politics
  translate.py       On-demand translation via OpenRouter, cached in translations
  static/            index.html (whole frontend), login.html
tests/test_scraper.py  Parser test against page_dump.html
```

The dashboard image copies `app/` and imports `app.database.engine`: changes to `app/database.py` affect both services.

## Commands

```bash
docker compose up -d --build              # run everything
docker compose logs -f app dashboard      # follow logs
docker compose exec db psql -U postgres -d 2chan_scraper
poetry run python -m pytest tests          # tests (scraper only); plain `pytest` fails: it can't import app and trips on postgres_data/
docker compose exec dashboard python -m dashboard.politics --dry-run --limit 20
```

The two services have separate dependency lists: `pyproject.toml` / `poetry.lock` for the scraper, `dashboard/requirements.txt` (pinned versions) for the dashboard.

## Conventions

- Python 3.11, plain functions and module-level constants, standard `logging` with f-strings.
- Comments explain *why*, briefly. Modules with non-obvious behavior start with a docstring describing it; keep those docstrings up to date.
- The dashboard uses raw SQL through `sqlalchemy.text()` with bound parameters, not the ORM. Never interpolate request values into SQL; the only f-string SQL (`politics_over_time`, `posts`, `threads`, `top_people`) interpolates constants.
- Frontend: a single static `index.html` calling `/api/*`. No build step.
- Commit messages follow Conventional Commits with a scope: `feat(dashboard): ...`, `chore: ...`.

## Things to know before changing code

- **No migrations.** The scraper creates its tables with `Base.metadata.create_all` (which never alters existing tables). Dashboard tables are created with `CREATE TABLE IF NOT EXISTS` in each module's `ensure_table()`. Changing a column of an existing table requires a manual `ALTER TABLE` on the live database.
- **Versioned derived data.** `TOKENIZER_VERSION` (tokenizer.py) and `CLASSIFIER_VERSION` (politics.py) must be bumped whenever the cleaning rules, people extraction rules (people.py), prompt or criteria change: workers redo the rows with an older version. Bumping `CLASSIFIER_VERSION` re-classifies every post through the paid API, so mention the cost before doing it.
- **Image classifier revision is pinned twice**: `MODEL_REVISION` in `dashboard/classifier.py` and the `MODEL_REVISION` build arg in `dashboard/Dockerfile`. The model is baked into the image and the container runs with `HF_HUB_OFFLINE=1`, so they must match.
- **Analyzed period.** Every dashboard query and worker filters on `posted_at >= START_DATE`. Keep that filter in new queries.
- **Time zone.** `posted_at` is naive Japan time parsed from the post header. Relative periods use `now() AT TIME ZONE 'Asia/Tokyo'`.
- **The mosaic fails closed.** Images that are unclassified or failed classification never go into the `safe` mosaic. Keep it that way.
- **Auth.** Every route except `/login` and `/healthz` requires a session (see the `require_login` middleware). New routes are protected automatically; do not add exceptions. Image routes validate filenames against `IMAGE_NAME` to prevent path traversal.
- **Workers** are daemon threads started in the FastAPI `lifespan`, each looping with its own sleep interval and logging (not raising) errors. Political classification also holds a Postgres advisory lock, so the CLI and the worker never run at the same time.
- **Scraper parsing** depends on 2chan's markup (`div.thre[data-res]`, `td.rtd`, `span.cno/cnm/csb/cnw`, `blockquote`). Pages are Shift_JIS. If parsing breaks, compare with a fresh page rather than editing `page_dump.html`, which the test depends on.

## Known issues

- `scrape_thread()` in `app/scraper.py` returns before its `time.sleep(2)`, so thread pages are fetched without delay.
- `from datetime import datetime` sits in the middle of `app/scraper.py`; it works because it runs at import time, before any function call.
- `pyproject.toml` still has placeholder authors.

## Data and secrets

- Never commit `.env`, `images/` or `postgres_data/` (all gitignored or untracked). `images/` and `postgres_data/` are owned by container users, so local tools may lack permission to read them.
- `images/` holds unfiltered user-posted content, possibly sensitive. Do not open, display or upload those files unless asked.
- Do not run the scraper outside Docker against the live site in loops, and do not lower the delays between requests.
