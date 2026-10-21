import hmac
import logging
import os
import re
import secrets
import time
from collections import Counter, defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.database import engine
from dashboard import classifier, mosaic, people, politics, tokenizer, translate
from dashboard.config import START_DATE

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

IMAGE_NAME = re.compile(r"\d+\.(jpe?g|png|gif|webp|mp4|webm)", re.IGNORECASE)

PERIODS = {"30d": 30, "90d": 90, "all": None}
_ngram_cache: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    people.ensure_table()
    tokenizer.ensure_table()
    tokenizer.start_worker()
    mosaic.start_worker()
    politics.ensure_table()
    politics.start_worker()
    translate.ensure_table()
    if not translate.enabled:
        logger.warning("OPENROUTER_KEY or OPENROUTER_MODEL is not set: translation disabled.")
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
                count(*) AS posts,
                count(DISTINCT thread_id) AS threads,
                count(*) FILTER (
                    WHERE posted_at >= (now() AT TIME ZONE 'Asia/Tokyo') - interval '7 days'
                ) AS posts_7d,
                min(posted_at) AS first_post,
                max(posted_at) AS last_post,
                (SELECT count(*) FROM post_tokens WHERE version = :version) AS tokenized,
                (SELECT count(*) FROM post_politics WHERE version = :politics_version) AS politics_classified
            FROM posts
            WHERE posted_at >= :start
        """), {"version": tokenizer.TOKENIZER_VERSION, "politics_version": politics.CLASSIFIER_VERSION,
              "start": START_DATE}).mappings().one()
    return dict(row)


@app.get("/api/posts-over-time")
def posts_over_time(granularity: str = Query("week", pattern="^(week|month)$")):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT date_trunc(:granularity, posted_at)::date AS period, count(*) AS posts
            FROM posts
            WHERE posted_at >= :start
            GROUP BY 1
            ORDER BY 1
        """), {"granularity": granularity, "start": START_DATE}).all()
    return [{"period": period.isoformat(), "posts": posts} for period, posts in rows]


@app.get("/api/politics-over-time")
def politics_over_time(
    granularity: str = Query("week", pattern="^(day|week|month)$"),
    min_confidence: float = Query(0, ge=0, le=1),
):
    # Labels come from a constant tuple, never from the request
    counts = ",\n".join(
        f"count(*) FILTER (WHERE c.label = '{label}' AND c.confidence >= :min_confidence) AS \"{label}\""
        for label in politics.LABELS
    )
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT
                date_trunc(:granularity, p.posted_at)::date AS period,
                count(*) AS total_posts,
                count(c.post_id) AS processed,
                count(c.post_id) FILTER (WHERE c.label <> :empty) AS classified,
                {counts}
            FROM posts p
            LEFT JOIN post_politics c ON c.post_id = p.id AND c.version = :version
            WHERE p.posted_at >= :start
            GROUP BY 1
            ORDER BY 1
        """), {
            "granularity": granularity, "min_confidence": min_confidence, "empty": politics.EMPTY_LABEL,
            "version": politics.CLASSIFIER_VERSION, "start": START_DATE,
        }).mappings().all()
    return [{**row, "period": row["period"].isoformat()} for row in rows]


POST_SELECT = """
    SELECT p.post_number, t.thread_number, t.subject, p.name, p.posted_at, p.content,
           p.local_image_path, p.is_op, c.label, c.confidence, c.probabilities,
           i.label AS image_label, i.nsfw_score
    FROM posts p
    JOIN threads t ON t.id = p.thread_id
    LEFT JOIN post_politics c ON c.post_id = p.id AND c.version = :version
    LEFT JOIN image_classifications i ON i.post_id = p.id AND i.model = :image_model
"""


def post_item(row) -> dict:
    item = dict(row)
    path = item.pop("local_image_path")
    item["image"] = Path(path).name if path else None
    item["posted_at"] = item["posted_at"].isoformat() if item["posted_at"] else None
    return item


def like_pattern(q: str) -> str:
    # The search text is matched literally, % and _ included
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@app.get("/api/posts")
def posts(
    label: str = Query("all"),
    min_confidence: float = Query(0, ge=0, le=1),
    q: str = Query("", max_length=200),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    sort: str = Query("recent", pattern="^(recent|oldest|confidence)$"),
    person: int | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    # "political" = any political label, "unclassified" = no up-to-date classification
    if label not in ("all", "political", "unclassified", politics.EMPTY_LABEL) + politics.LABELS:
        raise HTTPException(400, "invalid label")
    conditions = ["p.posted_at >= :start"]
    if label == "political":
        conditions.append("c.label = ANY(:political)")
    elif label == "unclassified":
        conditions.append("c.post_id IS NULL")
    elif label != "all":
        conditions.append("c.label = :label")
    if min_confidence:
        conditions.append("c.confidence >= :min_confidence")
    if q:
        conditions.append("p.content ILIKE :pattern")
    if person is not None:
        conditions.append(
            "EXISTS (SELECT 1 FROM post_person pp WHERE pp.post_id = p.id AND pp.person_id = :person)"
        )
    if date_from:
        conditions.append("p.posted_at >= :date_from")
    if date_to:
        # Inclusive: the whole last day (Japan time, like posted_at)
        conditions.append("p.posted_at < :date_to")
    order = {
        "recent": "p.posted_at DESC, p.id DESC",
        "oldest": "p.posted_at, p.id",
        "confidence": "c.confidence DESC NULLS LAST, p.id DESC",
    }[sort]
    # Conditions and order come from constants, never from the request
    stmt = text(f"""
        {POST_SELECT}
        WHERE {" AND ".join(conditions)}
        ORDER BY {order}
        OFFSET :offset LIMIT :limit
    """)
    with engine.connect() as conn:
        # One extra row tells whether there is a next page
        rows = conn.execute(stmt, {
            "start": START_DATE, "version": politics.CLASSIFIER_VERSION,
            "image_model": classifier.MODEL_ID, "political": list(politics.POLITICAL_LABELS),
            "date_from": date_from, "date_to": date_to and date_to + timedelta(days=1),
            "label": label, "min_confidence": min_confidence, "pattern": like_pattern(q),
            "person": person, "offset": offset, "limit": limit + 1,
        }).mappings().all()
    return {"items": [post_item(row) for row in rows[:limit]], "has_more": len(rows) > limit}


@app.get("/api/threads")
def threads(
    q: str = Query("", max_length=200),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    sort: str = Query("recent", pattern="^(recent|oldest|posts|political)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=100),
):
    # Threads with at least one post in the window; their stats cover the whole thread
    window = ["true"]
    if date_from:
        window.append("p.posted_at >= :date_from")
    if date_to:
        window.append("p.posted_at < :date_to")
    having = [f"bool_or({' AND '.join(window)})"]
    if q:
        having.append("(bool_or(p.content ILIKE :pattern) OR max(t.subject) ILIKE :pattern)")
    order = {
        "recent": "last_post DESC",
        "oldest": "first_post",
        "posts": "posts DESC",
        "political": "political_posts DESC",
    }[sort]
    counts = ",\n".join(
        f"count(*) FILTER (WHERE c.label = '{label}') AS \"{label}\"" for label in politics.LABELS
    )
    # Labels, conditions and order come from constants, never from the request
    stmt = text(f"""
        SELECT t.thread_number, t.subject, min(p.posted_at) AS first_post, max(p.posted_at) AS last_post,
               count(*) AS posts, count(p.local_image_path) AS images,
               (array_agg(p.content ORDER BY p.is_op DESC, p.posted_at, p.id))[1] AS first_content,
               bool_or(p.is_op) AS has_op,
               count(*) FILTER (WHERE c.label = ANY(:political)) AS political_posts,
               count(*) FILTER (WHERE c.label = ANY(:political))::real
                   / nullif(count(*) FILTER (WHERE c.label = ANY(:political) OR c.label = :non_political), 0)
                   AS political_share,
               {counts}
        FROM posts p
        JOIN threads t ON t.id = p.thread_id
        LEFT JOIN post_politics c ON c.post_id = p.id AND c.version = :version
        WHERE p.posted_at >= :start
        GROUP BY t.id
        HAVING {" AND ".join(having)}
        ORDER BY {order}, t.thread_number DESC
        OFFSET :offset LIMIT :limit
    """)
    with engine.connect() as conn:
        rows = conn.execute(stmt, {
            "start": START_DATE, "version": politics.CLASSIFIER_VERSION,
            "political": list(politics.POLITICAL_LABELS), "non_political": "non_political",
            "date_from": date_from, "date_to": date_to and date_to + timedelta(days=1),
            "pattern": like_pattern(q), "offset": offset, "limit": limit + 1,
        }).mappings().all()
    items = []
    for row in rows[:limit]:
        item = dict(row)
        item["first_post"] = item["first_post"].isoformat()
        item["last_post"] = item["last_post"].isoformat()
        item["first_content"] = (item["first_content"] or "")[:300]
        item["labels"] = {label: item.pop(label) for label in politics.LABELS}
        items.append(item)
    return {"items": items, "has_more": len(rows) > limit}


@app.get("/api/threads/{thread_number}")
def thread(thread_number: int):
    with engine.connect() as conn:
        info = conn.execute(text(
            "SELECT thread_number, subject FROM threads WHERE thread_number = :n"
        ), {"n": thread_number}).mappings().one_or_none()
        if info is None:
            raise HTTPException(404)
        rows = conn.execute(text(f"""
            {POST_SELECT}
            WHERE t.thread_number = :n AND p.posted_at >= :start
            ORDER BY p.posted_at, p.post_number
        """), {"n": thread_number, "start": START_DATE, "version": politics.CLASSIFIER_VERSION,
              "image_model": classifier.MODEL_ID}).mappings().all()
    return {**info, "posts": [post_item(row) for row in rows]}


def translated(source: str) -> dict:
    if not translate.enabled:
        raise HTTPException(503, "translation is not configured")
    try:
        return {"translation": translate.translate(source or ""), "model": translate.MODEL}
    except translate.TranslationError as e:
        logger.warning(f"Translation failed: {e}")
        raise HTTPException(502, "translation failed")


@app.post("/api/posts/{post_number}/translation")
def post_translation(post_number: int):
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT content FROM posts WHERE post_number = :n AND posted_at >= :start"
        ), {"n": post_number, "start": START_DATE}).one_or_none()
    if row is None:
        raise HTTPException(404)
    return translated(row.content)


@app.post("/api/threads/{thread_number}/translation")
def thread_translation(thread_number: int):
    # The title only: the frontend translates the posts one by one, to show them as they come
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT subject FROM threads WHERE thread_number = :n"
        ), {"n": thread_number}).one_or_none()
    if row is None:
        raise HTTPException(404)
    return translated(row.subject)


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
        "AND p.posted_at >= (now() AT TIME ZONE 'Asia/Tokyo') - make_interval(days => :days)"

    with engine.connect() as conn:
        # Cache key changes as soon as new posts get tokenized
        state = conn.execute(text(
            "SELECT count(*), coalesce(max(post_id), 0), coalesce(sum(version), 0) FROM post_tokens"
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
            WHERE p.posted_at >= :start {where}
        """), {"days": days, "start": START_DATE})

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


def display_names(conn, ids: list[int]) -> dict[int, str]:
    """Most frequent full form of each person (高市 -> 高市早苗), else their name."""
    rows = conn.execute(text("""
        SELECT pe.id, coalesce(f.form, pe.name)
        FROM person pe
        LEFT JOIN LATERAL (
            SELECT mode() WITHIN GROUP (ORDER BY form) AS form
            FROM post_person pp, unnest(pp.surface_forms) AS form
            WHERE pp.person_id = pe.id AND form <> pe.name
        ) f ON true
        WHERE pe.id = ANY(:ids)
    """), {"ids": ids})
    return dict(rows.all())


def top_people(conn, limit: int, days: int | None = None) -> list[dict]:
    """People named in the most posts, over the whole period or the last days."""
    where = "" if days is None else \
        "AND p.posted_at >= (now() AT TIME ZONE 'Asia/Tokyo') - make_interval(days => :days)"
    rows = conn.execute(text(f"""
        SELECT pe.id, pe.name, count(*) AS posts,
               count(DISTINCT p.thread_id) AS threads, sum(pp.mentions) AS mentions
        FROM post_person pp
        JOIN posts p ON p.id = pp.post_id
        JOIN person pe ON pe.id = pp.person_id
        WHERE p.posted_at >= :start {where}
        GROUP BY pe.id
        ORDER BY posts DESC, pe.id
        LIMIT :limit
    """), {"start": START_DATE, "days": days, "limit": limit}).mappings().all()
    names = display_names(conn, [row["id"] for row in rows])
    return [{**row, "display_name": names[row["id"]]} for row in rows]


@app.get("/api/people")
def people_ranking(
    period: str = Query("90d"),
    limit: int = Query(30, ge=5, le=100),
):
    if period not in PERIODS:
        raise HTTPException(400, f"period must be one of {list(PERIODS)}")
    with engine.connect() as conn:
        return {"items": top_people(conn, limit, PERIODS[period])}


@app.get("/api/people-over-time")
def people_over_time(
    granularity: str = Query("week", pattern="^(day|week|month)$"),
    top: int = Query(20, ge=1, le=50),
):
    """Posts naming each of the most discussed people (over all 2026), per period."""
    with engine.connect() as conn:
        ranked = top_people(conn, top)
        totals = conn.execute(text("""
            SELECT date_trunc(:granularity, posted_at)::date AS period, count(*)
            FROM posts
            WHERE posted_at >= :start
            GROUP BY 1
            ORDER BY 1
        """), {"granularity": granularity, "start": START_DATE}).all()
        counts = conn.execute(text("""
            SELECT date_trunc(:granularity, p.posted_at)::date AS period, pp.person_id, count(*)
            FROM post_person pp
            JOIN posts p ON p.id = pp.post_id
            WHERE p.posted_at >= :start AND pp.person_id = ANY(:ids)
            GROUP BY 1, 2
        """), {"granularity": granularity, "start": START_DATE,
               "ids": [person["id"] for person in ranked]}).all()
    by_key = {(period, person_id): n for period, person_id, n in counts}
    periods = [period for period, _ in totals]
    return {
        "periods": [period.isoformat() for period in periods],
        "total_posts": [n for _, n in totals],
        "people": [
            {"id": person["id"], "name": person["name"], "display_name": person["display_name"],
             "posts": [by_key.get((period, person["id"]), 0) for period in periods]}
            for person in ranked
        ],
    }


@app.get("/api/mosaic")
def mosaic_index():
    index = mosaic.load_index()
    return {"status": mosaic.status, "index": index}


@app.get("/mosaic/{set_name}/{layout}.jpg")
def mosaic_image(set_name: str, layout: str):
    if set_name not in mosaic.SETS or layout not in mosaic.LAYOUTS:
        raise HTTPException(404)
    path = mosaic.mosaic_file(set_name, layout)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, no-cache"})


@app.get("/images/{filename}")
def original_image(filename: str):
    if not IMAGE_NAME.fullmatch(filename):
        raise HTTPException(404)
    path = mosaic.IMAGE_DIR / filename
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})
