"""On-demand translation of posts and thread titles through OpenRouter.

Translations are requested from the dashboard (one call per post or title) and
cached in the `translations` table, keyed by a hash of the source text, the
model and the target language: the same text is never paid for twice, and a
change of model or language translates again.
"""
import hashlib
import json
import logging
import os
import urllib.error
import urllib.request

from sqlalchemy import text

from app.database import engine

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY = os.getenv("OPENROUTER_KEY")
MODEL = os.getenv("OPENROUTER_MODEL")
LANGUAGE = os.getenv("TRANSLATION_LANGUAGE", "French")
TIMEOUT_SECONDS = 60
MAX_CHARS = 4000

SYSTEM_PROMPT = (
    "You translate posts from a Japanese anonymous imageboard (2chan / futaba) "
    f"into {LANGUAGE}. Keep the line breaks and the leading '>' of quoted lines. "
    "Translate slang, memes and insults with their real meaning and tone, without "
    "softening them. Leave URLs and post references such as 'No.12345' unchanged. "
    "Reply with the translation only, without any comment."
)

enabled = bool(API_KEY and MODEL)


class TranslationError(Exception):
    pass


def ensure_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS translations (
                source_hash text NOT NULL,
                model text NOT NULL,
                language text NOT NULL,
                translation text NOT NULL,
                translated_at timestamp NOT NULL DEFAULT now(),
                PRIMARY KEY (source_hash, model, language)
            )
        """))


def call_api(source: str) -> str:
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": source[:MAX_CHARS]},
        ],
        "temperature": 0,
    }).encode()
    request = urllib.request.Request(API_URL, data=body, headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.load(response)
        return data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        # The body explains the error (invalid key, unknown model, no credits...)
        raise TranslationError(f"OpenRouter {e.code}: {e.read()[:300].decode(errors='replace')}")
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError) as e:
        raise TranslationError(f"OpenRouter: {e}")


def translate(source: str) -> str:
    """Translation of `source`, from the cache when it was already translated."""
    if not source.strip():
        return ""
    key = {"hash": hashlib.sha256(source.encode()).hexdigest(), "model": MODEL, "language": LANGUAGE}
    with engine.connect() as conn:
        cached = conn.execute(text("""
            SELECT translation FROM translations
            WHERE source_hash = :hash AND model = :model AND language = :language
        """), key).scalar()
    if cached is not None:
        return cached
    translation = call_api(source)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO translations (source_hash, model, language, translation)
            VALUES (:hash, :model, :language, :translation)
            ON CONFLICT DO NOTHING
        """), {**key, "translation": translation})
    return translation
