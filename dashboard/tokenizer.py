"""Japanese tokenization of posts, cached in the `post_tokens` table.

2chan posts are Japanese: there are no spaces between words, so n-grams must be
built on top of a morphological analyzer (MeCab via fugashi + UniDic) rather
than on `str.split()`.

The same MeCab pass also extracts the people named in each post (see
people.py), stored in the `person` and `post_person` tables.

Many posts are partly automatic or templated (e.g. a daily "ログインボーナス"
digest that re-posts the same headers and headlines every day). To keep them
from dominating the rankings, a long line is only tokenized in the first post
where it appears: later copies of that line are ignored.
"""
import logging
import re
import threading
import time
import unicodedata

from fugashi import Tagger
from sqlalchemy import text

from app.database import engine
from dashboard import people
from dashboard.config import START_DATE

logger = logging.getLogger(__name__)

# Bump this when the cleaning/filtering rules or the people extraction rules
# change: posts tokenized with an older version are re-tokenized by the worker.
TOKENIZER_VERSION = 6
BATCH_SIZE = 1000
IDLE_SECONDS = 300

# Parts of speech kept as content words (UniDic pos1)
KEPT_POS = {"名詞", "動詞", "形容詞", "形状詞"}
# Sub-categories that carry little meaning on their own
DROPPED_POS2 = {"数詞", "非自立可能"}

STOPWORDS = {
    "こと", "もの", "よう", "ため", "ところ", "感じ", "方", "時", "今", "奴", "やつ",
    "思う", "言う", "見る", "分かる", "わかる", "知る", "出る", "行く", "来る",
    "やる", "いう", "なん", "そう", "どう", "ここ", "そこ", "あれ", "これ", "それ",
    "年", "月", "日", "午前", "午後", "new",
    "the", "a", "of", "to", "and", "in", "is",
}

QUOTE_LINE = re.compile(r"^\s*[>＞]")
# 2chan system messages left in place of deleted posts
DELETED_LINE = re.compile(r"によって削除されました\s*$")
# Headers of posts copied from 5ch: "055 番組の途中ですが 2026/05/30(土) ID:Y2vBUiAX0"
COPIED_HEADER_LINE = re.compile(r"\d{4}/\d{1,2}/\d{1,2}\(.\).*ID:")
URL = re.compile(r"https?://\S+|www\.\S+")
NOISE = re.compile(
    r"No\.\d+"                                      # post references
    r"|ID:\S+"                                      # poster IDs
    r"|\d{2,4}/\d{1,2}/\d{1,2}(\(.\))?"             # dates
    r"|\d{1,2}:\d{2}(:\d{2})?"                      # times
)
# Lines shorter than this (reactions like "草" or "それな") are never deduplicated
MIN_DEDUP_LINE_LENGTH = 8
# Bullets and markers that change between copies of a same line ("(new)" then "・")
LINE_PREFIX = re.compile(r"^[\s・*\-●○■□◆◇★☆]*(\(new\))?[\s・]*", re.IGNORECASE)
SKIP_TOKEN = re.compile(r"^([\W\d_]+|[ぁ-ゖー]|[a-z])$")

_tagger = None


def get_tagger() -> Tagger:
    global _tagger
    if _tagger is None:
        _tagger = Tagger()
    return _tagger


def normalize(content: str) -> str:
    return unicodedata.normalize("NFKC", content or "")


def line_key(line: str) -> str | None:
    """Identity of a line for deduplication, None if it is too short to matter."""
    key = re.sub(r"\s+", "", LINE_PREFIX.sub("", line))
    return key if len(key) >= MIN_DEDUP_LINE_LENGTH else None


def kept_lines(content: str, first_post: dict[str, int], post_id: int) -> list[str]:
    """Lines worth analyzing: not quoted, not a system message, not a copy."""
    lines = []
    for line in normalize(content).splitlines():
        if QUOTE_LINE.match(line) or DELETED_LINE.search(line) or COPIED_HEADER_LINE.search(line):
            continue
        key = line_key(line)
        if key is not None and first_post.get(key, post_id) != post_id:
            continue
        lines.append(line)
    return lines


def clean(text_: str) -> str:
    """Remove URLs and metadata."""
    return NOISE.sub(" ", URL.sub(" ", text_))


def analyze(text_: str) -> tuple[list[str], dict[str, list[str]]]:
    """Content words and people named in a text, from a single MeCab pass."""
    words = get_tagger()(clean(text_))
    return content_words(words), people.extract_people(words)


def content_words(words) -> list[str]:
    """Return the sequence of content words (dictionary form) of tagged words."""
    tokens = []
    for word in words:
        feature = word.feature
        pos1 = getattr(feature, "pos1", None)
        pos2 = getattr(feature, "pos2", None)
        if pos1 not in KEPT_POS or pos2 in DROPPED_POS2:
            continue
        base = getattr(feature, "orthBase", None)
        if not base or base == "*":
            base = word.surface
        base = base.lower()
        if base in STOPWORDS or SKIP_TOKEN.match(base):
            continue
        tokens.append(base)
    return tokens


def ensure_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS post_tokens (
                post_id integer PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
                version smallint NOT NULL,
                tokens text[] NOT NULL
            )
        """))


def first_post_by_line() -> dict[str, int]:
    """Map each long line to the id of the first post containing it."""
    first_post = {}
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, content FROM posts
            WHERE posted_at >= :start AND content <> ''
            ORDER BY id
        """), {"start": START_DATE})
        for post_id, content in rows:
            for line in normalize(content).splitlines():
                key = line_key(line)
                if key is not None:
                    first_post.setdefault(key, post_id)
    return first_post


def tokenize_pending(first_post: dict[str, int]) -> int:
    """Tokenize the posts that are new or were tokenized with an old version."""
    total = 0
    while True:
        with engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT p.id, p.content
                FROM posts p
                LEFT JOIN post_tokens t ON t.post_id = p.id
                WHERE p.posted_at >= :start
                  AND (t.post_id IS NULL OR t.version < :version)
                ORDER BY p.id
                LIMIT :limit
            """), {"start": START_DATE, "version": TOKENIZER_VERSION, "limit": BATCH_SIZE}).all()
            if not rows:
                return total
            results = {
                post_id: analyze("\n".join(kept_lines(content, first_post, post_id)))
                for post_id, content in rows
            }
            conn.execute(text("""
                INSERT INTO post_tokens (post_id, version, tokens)
                VALUES (:post_id, :version, :tokens)
                ON CONFLICT (post_id) DO UPDATE
                SET version = EXCLUDED.version, tokens = EXCLUDED.tokens
            """), [
                {"post_id": post_id, "version": TOKENIZER_VERSION, "tokens": tokens}
                for post_id, (tokens, _) in results.items()
            ])
            people.save_people(conn, {
                post_id: named for post_id, (_, named) in results.items()
            })
        total += len(rows)


def delete_out_of_range():
    with engine.begin() as conn:
        conn.execute(text("""
            DELETE FROM post_tokens t USING posts p
            WHERE p.id = t.post_id AND (p.posted_at IS NULL OR p.posted_at < :start)
        """), {"start": START_DATE})
        conn.execute(text("""
            DELETE FROM post_person pp USING posts p
            WHERE p.id = pp.post_id AND (p.posted_at IS NULL OR p.posted_at < :start)
        """), {"start": START_DATE})


def _worker():
    while True:
        try:
            ensure_table()
            people.ensure_table()
            delete_out_of_range()
            total = tokenize_pending(first_post_by_line())
            # After tokenizing: re-processed posts may have dropped the last mention of someone
            with engine.begin() as conn:
                people.delete_orphans(conn)
            if total:
                logger.info(f"Tokenized {total} posts.")
        except Exception as e:
            logger.error(f"Tokenization failed: {e}")
        time.sleep(IDLE_SECONDS)


def start_worker():
    threading.Thread(target=_worker, name="tokenizer", daemon=True).start()
