"""Political classification of posts with TypeSafe AI (Jev decision model).

Each post is classified into one of: non_political, far_left, left, center,
right, far_right. The chosen label, its confidence and the probabilities of
every label are stored in the `post_politics` table, so the dashboard can
filter on confidence without classifying again.

Run once by hand (e.g. for the initial backfill or to try the criteria):
    python -m dashboard.politics [--limit N] [--dry-run]
"""
import argparse
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import text

from app.database import engine
from dashboard.config import START_DATE
from dashboard.tokenizer import DELETED_LINE, QUOTE_LINE, URL, normalize

logger = logging.getLogger(__name__)

MODEL = "jev-1.13.0"
# Bump this when the question or the criteria change: posts classified with an
# older version are classified again by the background worker.
CLASSIFIER_VERSION = 1
POLITICAL_LABELS = ("far_left", "left", "center", "right", "far_right")
LABELS = ("non_political",) + POLITICAL_LABELS
# Posts without any text left once cleaned: recorded without calling the API
EMPTY_LABEL = "empty"

API_KEY = os.getenv("JEV_KEY")
CONCURRENCY = int(os.getenv("POLITICS_CONCURRENCY", "8"))
BATCH_SIZE = 200
IDLE_SECONDS = 300
MAX_CHARS = 2000
# Arbitrary key of the Postgres advisory lock: a single process classifies at a time
LOCK_KEY = 7_351_002

INSTRUCTIONS = (
    "This is a post from a Japanese anonymous imageboard (2chan / futaba). "
    "Classify the political stance expressed by the author of the post, in the "
    "context of Japanese politics. Judge the opinion the author expresses, not "
    "the topic that is mentioned: a neutral news headline or a question is not "
    "a stance. The text under 'Replying to:' quotes other posts: use it only "
    "as context, the stance must come from the author's own text. Note that on "
    "Japanese imageboards, attacks on the mainstream media (マスゴミ, 偏向報道, "
    "報道しない自由), support for Abe or Takaichi and mockery of 'パヨク' are "
    "right-wing markers, while attacks on the LDP, 統一教会 links or 'ネトウヨ' "
    "are left-wing markers. Posts without any political opinion are non_political."
)

CRITERIA = {
    "non_political": {
        "what": "No political opinion: daily chat, games, anime, food, jokes, "
                "reactions, image comments, or politics mentioned without taking a side",
        "examples": ["草", "今日の晩飯うまかった", "このキャラかわいい", "選挙いつだっけ"],
    },
    "far_left": {
        "what": "Radical or revolutionary left: abolition of the emperor system, "
                "anti-capitalist revolution, militant anti-Japan or anti-state positions",
        "examples": ["天皇制は廃止すべき", "資本主義を打倒しろ", "日本という国家そのものが悪"],
    },
    "left": {
        "what": "Mainstream left / liberal: support for CDP, JCP, Social Democrats or "
                "Reiwa Shinsengumi, defending Article 9 (護憲), anti-nuclear, social welfare, "
                "minority rights, criticism of the LDP or of nationalists from the left",
        "examples": ["憲法9条を守れ", "原発はもう止めるべき", "自民党は格差を広げただけ",
                     "統一教会とズブズブの自民党", "ネトウヨが騒いでる"],
    },
    "center": {
        "what": "Moderate or mixed political opinion, pragmatic or criticising both sides",
        "examples": ["与党も野党もどっちもどっち", "現実的な落とし所を探すべき"],
    },
    "right": {
        "what": "Mainstream conservative: support for the LDP, Sanseito or other conservative "
                "parties, constitutional revision (改憲), stronger defence, traditional values, "
                "distrust of the 'leftist' mainstream media, criticism of the opposition from the right",
        "examples": ["防衛費増額は当然", "改憲して自衛隊を明記すべき", "野党は反対ばかり",
                     "マスゴミの偏向報道がひどい", "報道しない自由で誤魔化してる"],
    },
    "far_right": {
        "what": "Radical nationalist right (ネトウヨ): xenophobia, hostility or slurs against "
                "Koreans, Chinese or foreigners, 在日 conspiracy theories, calls to expel "
                "immigrants, historical revisionism, calling opponents 売国奴 or 反日",
        "examples": ["在日は国に帰れ", "移民を全員追い出せ", "パヨクは売国奴", "南京大虐殺は捏造"],
    },
}

# A line that is only a reference to another post ("No.2752237")
POST_REFERENCE = re.compile(r"No\.\d+")

_local = threading.local()


def ensure_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS post_politics (
                post_id integer PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
                version smallint NOT NULL,
                model text NOT NULL,
                label text NOT NULL,
                confidence real,
                probabilities jsonb NOT NULL,
                classified_at timestamp NOT NULL DEFAULT now()
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS post_politics_label_idx ON post_politics (label)"
        ))


def get_client():
    # The SDK recommends one client per thread
    if not hasattr(_local, "client"):
        from typesafe_sdk import TypeSafeClient
        _local.client = TypeSafeClient(api_key=API_KEY, model=MODEL, timeout=30)
    return _local.client


def prepare(content: str) -> str:
    """Text sent to the model: the author's own lines first, then the quoted
    lines as context. Empty if the author wrote nothing (image-only post)."""
    own, quoted = [], []
    for line in normalize(content).splitlines():
        line = URL.sub("", line).strip()
        if DELETED_LINE.search(line):
            continue
        if QUOTE_LINE.match(line):
            line = QUOTE_LINE.sub("", line).lstrip(">＞ ")
            target = quoted
        else:
            target = own
        if line and not POST_REFERENCE.fullmatch(line):
            target.append(line)
    if not own:
        return ""
    post_text = "\n".join(own)
    if quoted:
        post_text += "\n\nReplying to:\n" + "\n".join(quoted)
    return post_text[:MAX_CHARS]


def classify(post_text: str) -> dict:
    from typesafe_sdk import Choice

    response = get_client().system_one(
        state=post_text,
        questions={"leaning": Choice(instructions=INSTRUCTIONS, criteria=CRITERIA)},
    )
    answer = response.answers["leaning"]
    return {
        "label": answer.choice,
        "confidence": round(float(answer.confidence), 4),
        "probabilities": {k: round(float(v), 4) for k, v in answer.probabilities.items()},
    }


def pending_posts(after_id: int, limit: int) -> list[tuple[int, str]]:
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT p.id, p.content
            FROM posts p
            LEFT JOIN post_politics c ON c.post_id = p.id
            WHERE p.posted_at >= :start AND p.id > :after_id
              AND (c.post_id IS NULL OR c.version < :version)
            ORDER BY p.id
            LIMIT :limit
        """), {"start": START_DATE, "version": CLASSIFIER_VERSION,
               "after_id": after_id, "limit": limit}).all()


def count_pending() -> int:
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT count(*) FROM posts p
            LEFT JOIN post_politics c ON c.post_id = p.id
            WHERE p.posted_at >= :start
              AND (c.post_id IS NULL OR c.version < :version)
        """), {"start": START_DATE, "version": CLASSIFIER_VERSION}).scalar()


def save(records: list[dict]):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO post_politics (post_id, version, model, label, confidence, probabilities)
            VALUES (:post_id, :version, :model, :label, :confidence, CAST(:probabilities AS jsonb))
            ON CONFLICT (post_id) DO UPDATE
            SET version = EXCLUDED.version, model = EXCLUDED.model, label = EXCLUDED.label,
                confidence = EXCLUDED.confidence, probabilities = EXCLUDED.probabilities,
                classified_at = now()
        """), [
            {**r, "version": CLASSIFIER_VERSION, "probabilities": json.dumps(r["probabilities"])}
            for r in records
        ])


def classify_batch(rows: list[tuple[int, str]], executor: ThreadPoolExecutor) -> tuple[list[dict], int]:
    """Classify a batch of posts. Returns the records and the number of failures."""
    records, futures = [], {}
    for post_id, content in rows:
        post_text = prepare(content)
        if post_text:
            futures[executor.submit(classify, post_text)] = post_id
        else:
            records.append({"post_id": post_id, "model": MODEL, "label": EMPTY_LABEL,
                            "confidence": None, "probabilities": {}})
    failed = 0
    for future in as_completed(futures):
        post_id = futures[future]
        try:
            records.append({"post_id": post_id, "model": MODEL, **future.result()})
        except Exception as e:
            # Not recorded: the post is retried on the next cycle
            logger.warning(f"Cannot classify post {post_id}: {e}")
            failed += 1
    return records, failed


def classify_pending(limit: int | None = None) -> int:
    """Classify the posts of the analyzed period that are new or outdated."""
    with engine.connect() as lock_conn:
        if not lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
            logger.info("Political classification already running in another process.")
            return 0
        # The lock belongs to the session: don't stay idle in transaction meanwhile
        lock_conn.commit()
        try:
            total = count_pending() if limit is None else min(limit, count_pending())
            if not total:
                return 0
            logger.info(f"Classifying {total} posts politically...")
            done, after_id = 0, 0
            with ThreadPoolExecutor(CONCURRENCY) as executor:
                while done < total:
                    # Keyset pagination: posts that failed are skipped until the next cycle
                    rows = pending_posts(after_id, min(BATCH_SIZE, total - done))
                    if not rows:
                        break
                    records, failed = classify_batch(rows, executor)
                    if records:
                        save(records)
                    done += len(rows)
                    after_id = rows[-1][0]
                    logger.info(f"Political classification: {done}/{total}"
                                + (f" ({failed} failed)" if failed else ""))
                    if not records:
                        # Every call failed (API down, invalid key...): retry later
                        break
            return done
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})


def delete_out_of_range():
    with engine.begin() as conn:
        conn.execute(text("""
            DELETE FROM post_politics c USING posts p
            WHERE p.id = c.post_id AND (p.posted_at IS NULL OR p.posted_at < :start)
        """), {"start": START_DATE})


def _worker():
    while True:
        try:
            ensure_table()
            delete_out_of_range()
            classify_pending()
        except Exception as e:
            logger.error(f"Political classification failed: {e}")
        time.sleep(IDLE_SECONDS)


def start_worker():
    if not API_KEY:
        logger.warning("JEV_KEY is not set: political classification disabled.")
        return
    threading.Thread(target=_worker, name="politics", daemon=True).start()


def dry_run(limit: int):
    """Print the classification of the latest posts without saving it."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, content FROM posts
            WHERE posted_at >= :start AND content <> ''
            ORDER BY id DESC LIMIT :limit
        """), {"start": START_DATE, "limit": limit}).all()
    with ThreadPoolExecutor(CONCURRENCY) as executor:
        records, _ = classify_batch(rows, executor)
    contents = dict(rows)
    for r in sorted(records, key=lambda r: r["post_id"]):
        preview = prepare(contents[r["post_id"]]).replace("\n", " / ")[:120]
        print(f"#{r['post_id']} {r['label']:<13} conf={r['confidence']}  {preview}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, help="classify at most N posts")
    parser.add_argument("--dry-run", action="store_true", help="print the latest posts' labels without saving")
    args = parser.parse_args()
    if not API_KEY:
        raise SystemExit("JEV_KEY must be set")
    if args.dry_run:
        dry_run(args.limit or 20)
    else:
        ensure_table()
        classify_pending(args.limit)
