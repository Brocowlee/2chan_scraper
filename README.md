# 2chan Scraper

Scrapes board 35 of [2chan / futaba](https://nov.2chan.net/35/futaba.htm) into PostgreSQL, downloads the images posted there, and serves a password-protected dashboard to analyze the collected posts.

## Features

- **Scraper**: every `SCRAPE_INTERVAL_MINUTES`, scrapes index pages 0–10, then every thread found on them. Threads and posts are upserted into PostgreSQL, and images are saved to `images/`.
- **Dashboard** (FastAPI with a single-page frontend):
  - Summary stats and posts over time
  - Top Japanese bigrams and trigrams (MeCab via fugashi + UniDic), with copy-pasted and templated lines deduplicated
  - Most discussed people, from the person names tagged by UniDic (grouped by surname), with a person filter in the posts browser
  - Image mosaics (chronological and sorted by color), filtered by a local ViT image classifier
  - Political leaning of posts over time (non-political, far left … far right), classified with the TypeSafe AI Jev model

## Architecture

```
            ┌───────────┐  posts, threads   ┌────────────┐
2chan  ───▶ │  app      │ ────────────────▶ │  db        │
            │ (scraper) │                   │ Postgres 15│
            └─────┬─────┘                   └─────┬──────┘
                  │ images/                       │ posts + derived tables
                  ▼                               ▼
            ┌──────────────────────────────────────────┐
            │  dashboard (FastAPI, 127.0.0.1:8090)     │
            │  background workers: tokenizer, mosaic + │
            │  image classifier, political classifier  │
            └──────────────────────────────────────────┘
```

| Service     | Code          | Role                                                     |
|-------------|---------------|----------------------------------------------------------|
| `app`       | `app/`        | Scraper loop (`python -m app.main`)                      |
| `db`        | —             | PostgreSQL, data in `./postgres_data`                    |
| `dashboard` | `dashboard/`  | Web UI and API, plus background workers that fill the derived tables |

The dashboard only analyzes posts published from `DASHBOARD_START_DATE` (default `2026-01-01`). Earlier posts were only partially collected.

## Getting started

Requirements: Docker with Docker Compose.

```bash
cp .env.example .env
# Set at least DASHBOARD_PASSWORD and DASHBOARD_SESSION_SECRET:
openssl rand -hex 32
docker compose up -d --build
```

The dashboard is then available at http://127.0.0.1:8090. It only listens on localhost; it is meant to be exposed publicly through a Cloudflare tunnel.

> Session cookies are `Secure` by default, so logging in over plain HTTP fails. For local use without HTTPS, set `DASHBOARD_SECURE_COOKIE=false` on the dashboard service.

## Configuration

Set in `.env` (see `.env.example`):

| Variable                    | Used by       | Description                                                  |
|-----------------------------|---------------|--------------------------------------------------------------|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `POSTGRES_HOST` / `POSTGRES_PORT` | all | Database connection |
| `SCRAPE_INTERVAL_MINUTES`   | app           | Delay between scrape cycles (default 15)                     |
| `DASHBOARD_PASSWORD`        | dashboard     | Login password (required)                                    |
| `DASHBOARD_SESSION_SECRET`  | dashboard     | Session signing key. Without it, sessions are lost on every restart |
| `JEV_KEY`                   | dashboard     | TypeSafe AI key. Political classification is disabled when empty |
| `OPENROUTER_KEY` / `OPENROUTER_MODEL` | dashboard | OpenRouter key and model id used by the "Traduire" buttons. Translation is disabled when either is empty |

Optional dashboard settings, read from the container environment (add them to `docker-compose.yml` to use them):

| Variable                    | Default        | Description                                     |
|-----------------------------|----------------|-------------------------------------------------|
| `DASHBOARD_START_DATE`      | `2026-01-01`   | First day of the analyzed period                |
| `DASHBOARD_FILTER_THRESHOLD`| `0.5`          | Classifier score above which an image is filtered out |
| `TRANSLATION_LANGUAGE`      | `French`       | Target language of translations                 |
| `DASHBOARD_TORCH_THREADS`   | `3`            | CPU threads used by the image classifier        |
| `DASHBOARD_SECURE_COOKIE`   | `true`         | Send the session cookie over HTTPS only         |
| `POLITICS_CONCURRENCY`      | `8`            | Parallel requests to the TypeSafe API           |
| `DASHBOARD_IMAGE_DIR`       | `/srv/images`  | Scraped images (mounted read-only)              |
| `DASHBOARD_CACHE_DIR`       | `/srv/cache`   | Thumbnails, mosaics and their index             |

## Database

Tables created by the scraper (SQLAlchemy models in `app/models.py`):

- `threads`: one row per thread (`thread_number`, `subject`, …)
- `posts`: one row per post (`post_number`, `content`, `posted_at`, `image_url`, `local_image_path`, `is_op`, …)

Derived tables, created and filled by the dashboard workers:

- `post_tokens`: tokenized content of each post, used for n-grams
- `person`: one row per person named in a post (keyed by surname)
- `post_person`: n-n link between `posts` and `person`, with the number of mentions and the forms as written
- `image_classifications`: classifier scores of each image
- `post_politics`: political label, confidence and probabilities of each post

`posted_at` is the time shown on 2chan, i.e. Japan time (stored without a time zone).

## Running tasks by hand

Backfill or test the political classification:

```bash
docker compose exec dashboard python -m dashboard.politics --dry-run --limit 20  # print labels, save nothing
docker compose exec dashboard python -m dashboard.politics --limit 1000           # classify and save
```

## Development

```bash
poetry install
poetry run python -m pytest tests
```

The test parses `page_dump.html`, a saved 2chan page.

## Responsible use

This project collects public posts from an anonymous imageboard for analysis. Keep the scraping rate low (the scraper waits 2 seconds between index pages), and keep the dashboard behind its login: the image store contains unfiltered user content.
