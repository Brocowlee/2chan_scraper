import hmac
import logging
import os
import secrets
import time
from collections import Counter, defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.database import engine
from dashboard import tokenizer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
if not DASHBOARD_PASSWORD:
    raise RuntimeError("DASHBOARD_PASSWORD must be set")
# Without a fixed secret, sessions are invalidated on every restart
SESSION_SECRET = os.getenv("DASHBOARD_SESSION_SECRET") or secrets.token_urlsafe(32)
SECURE_COOKIE = os.getenv("DASHBOARD_SECURE_COOKIE", "true").lower() == "true"

# Login throttling: max failed attempts per client IP within the window
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60
_failures: dict[str, deque] = defaultdict(deque)

PERIODS = {"30d": 30, "90d": 90, "365d": 365, "all": None}
_ngram_cache: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    tokenizer.ensure_table()
    tokenizer.start_worker()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.url.path in ("/login", "/healthz") or request.session.get("auth"):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


# Added after the auth middleware so that it wraps it (session is decoded first)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="dashboard_session",
    max_age=7 * 24 * 3600,
    same_site="strict",
    https_only=SECURE_COOKIE,
)


def client_ip(request: Request) -> str:
    # Set by Cloudflare when the request comes through the tunnel
    return request.headers.get("cf-connecting-ip") or request.client.host


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/login")
def login_page():
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    ip = client_ip(request)
    attempts = _failures[ip]
    now = time.monotonic()
    while attempts and now - attempts[0] > FAILURE_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= MAX_FAILURES:
        return RedirectResponse("/login?error=locked", status_code=303)

    if hmac.compare_digest(password.encode(), DASHBOARD_PASSWORD.encode()):
        attempts.clear()
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)

    attempts.append(now)
    logger.warning(f"Failed login attempt from {ip}")
    time.sleep(1)
    return RedirectResponse("/login?error=invalid", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/summary")
def summary():
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT
                (SELECT count(*) FROM posts) AS posts,
                (SELECT count(*) FROM threads) AS threads,
                (SELECT count(*) FROM posts
                 WHERE posted_at >= (now() AT TIME ZONE 'Asia/Tokyo') - interval '7 days') AS posts_7d,
                (SELECT min(posted_at) FROM posts) AS first_post,
                (SELECT max(posted_at) FROM posts) AS last_post,
                (SELECT count(*) FROM post_tokens WHERE version = :version) AS tokenized
        """), {"version": tokenizer.TOKENIZER_VERSION}).mappings().one()
    return dict(row)


@app.get("/api/posts-over-time")
def posts_over_time(granularity: str = Query("week", pattern="^(week|month)$")):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT date_trunc(:granularity, posted_at)::date AS period, count(*) AS posts
            FROM posts
            WHERE posted_at IS NOT NULL
            GROUP BY 1
            ORDER BY 1
        """), {"granularity": granularity}).all()
    return [{"period": period.isoformat(), "posts": posts} for period, posts in rows]


@app.get("/api/ngrams")
def ngrams(
    n: int = Query(2, ge=2, le=3),
    period: str = Query("90d"),
    limit: int = Query(30, ge=5, le=100),
):
    if period not in PERIODS:
        raise HTTPException(400, f"period must be one of {list(PERIODS)}")
    days = PERIODS[period]
    where = "" if days is None else \
        "WHERE p.posted_at >= (now() AT TIME ZONE 'Asia/Tokyo') - make_interval(days => :days)"

    with engine.connect() as conn:
        # Cache key changes as soon as new posts get tokenized
        state = conn.execute(text(
            "SELECT count(*), coalesce(max(post_id), 0) FROM post_tokens"
        )).one()
        state = tuple(state)
        if _ngram_cache.get("state") != state:
            _ngram_cache.clear()
            _ngram_cache["state"] = state
        key = (n, period, limit)
        if key in _ngram_cache:
            return _ngram_cache[key]

        rows = conn.execute(text(f"""
            SELECT t.tokens FROM post_tokens t
            JOIN posts p ON p.id = t.post_id
            {where}
        """), {"days": days})

        # Count each n-gram once per post, so a copypasta repeated
        # many times in a single post doesn't dominate the ranking
        counter = Counter()
        posts = 0
        for (tokens,) in rows:
            posts += 1
            counter.update({
                " ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)
            })

    result = {
        "posts": posts,
        "items": [{"ngram": g, "posts": c} for g, c in counter.most_common(limit)],
    }
    _ngram_cache[key] = result
    return result
