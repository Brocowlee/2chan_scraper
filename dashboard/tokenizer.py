"""Japanese tokenization of posts, cached in the `post_tokens` table.

2chan posts are Japanese: there are no spaces between words, so n-grams must be
built on top of a morphological analyzer (MeCab via fugashi + UniDic) rather
than on `str.split()`.
"""
import logging
import re
import threading
import time
import unicodedata

from fugashi import Tagger
from sqlalchemy import text

from app.database import engine

logger = logging.getLogger(__name__)

# Bump this when the cleaning/filtering rules change: posts tokenized with an
# older version are re-tokenized by the background worker.
TOKENIZER_VERSION = 2
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
    "年", "月", "日", "午前", "午後",
    "the", "a", "of", "to", "and", "in", "is",
}

QUOTE_LINE = re.compile(r"^\s*[>＞]")
# 2chan system messages left in place of deleted posts
DELETED_LINE = re.compile(r"によって削除されました\s*$")
URL = re.compile(r"https?://\S+|www\.\S+")
NOISE = re.compile(
    r"No\.\d+"                                      # post references
    r"|ID:\S+"                                      # poster IDs
    r"|\d{2,4}/\d{1,2}/\d{1,2}(\(.\))?"             # dates
    r"|\d{1,2}:\d{2}(:\d{2})?"                      # times
)
SKIP_TOKEN = re.compile(r"^([\W\d_]+|[ぁ-ゖー]|[a-z])$")

_tagger = None


def get_tagger() -> Tagger:
    global _tagger
    if _tagger is None:
        _tagger = Tagger()
    return _tagger


def clean(content: str) -> str:
    """Remove quoted lines (they duplicate other posts), URLs and metadata."""
    content = unicodedata.normalize("NFKC", content or "")
    lines = [line for line in content.splitlines()
             if not QUOTE_LINE.match(line) and not DELETED_LINE.search(line)]
    content = "\n".join(lines)
    content = URL.sub(" ", content)
    return NOISE.sub(" ", content)


def tokenize(content: str) -> list[str]:
    """Return the sequence of content words (dictionary form) of a post."""
    tokens = []
    for word in get_tagger()(clean(content)):
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


def tokenize_pending_batch() -> int:
    """Tokenize one batch of posts that are new or tokenized with an old version."""
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT p.id, p.content
            FROM posts p
            LEFT JOIN post_tokens t ON t.post_id = p.id
            WHERE t.post_id IS NULL OR t.version < :version
            ORDER BY p.id
            LIMIT :limit
        """), {"version": TOKENIZER_VERSION, "limit": BATCH_SIZE}).all()
        if not rows:
            return 0
        conn.execute(text("""
            INSERT INTO post_tokens (post_id, version, tokens)
            VALUES (:post_id, :version, :tokens)
            ON CONFLICT (post_id) DO UPDATE
            SET version = EXCLUDED.version, tokens = EXCLUDED.tokens
        """), [
            {"post_id": post_id, "version": TOKENIZER_VERSION, "tokens": tokenize(content)}
            for post_id, content in rows
        ])
    return len(rows)


def _worker():
    while True:
        try:
            ensure_table()
            total = 0
            while (done := tokenize_pending_batch()) > 0:
                total += done
            if total:
                logger.info(f"Tokenized {total} posts.")
        except Exception as e:
            logger.error(f"Tokenization failed: {e}")
        time.sleep(IDLE_SECONDS)


def start_worker():
    threading.Thread(target=_worker, name="tokenizer", daemon=True).start()
