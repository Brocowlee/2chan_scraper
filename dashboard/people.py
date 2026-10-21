"""People mentioned in posts, from the person names tagged by UniDic.

Filled by the tokenizer worker (one MeCab pass per post), stored in two tables:
`person` (one row per person) and `post_person` (n-n link with `posts`).

UniDic splits a name into tokens tagged 固有名詞/人名 with a pos4 of 姓
(surname), 名 (given name) or 一般 (mostly foreign names). A run of such tokens,
optionally joined by "・" (ドナルド・トランプ), is one name. A surname always
starts a new name, so listed surnames (岸田石破) count as several people.
Posters mostly write the surname alone, so a person is keyed by:
  - the surname when there is one (高市早苗, 高市 -> 高市),
  - otherwise the last part of the name (ジェフリー・エプスタイン -> エプスタイン).
Given names written alone (早苗) are ambiguous and ignored. Different people
sharing a surname are merged; the forms as written are kept in `surface_forms`.

Bump TOKENIZER_VERSION (tokenizer.py) when these rules change.
"""
from collections import defaultdict

from sqlalchemy import text

from app.database import engine

# Names UniDic tags as common nouns (トランプ is the card game)
EXTRA_NAMES = {"トランプ"}
# Tagged as person names but are not people anyone discusses as such
NOT_PERSONS = {"キリスト", "基督", "コンゴ", "ノーベル", "ウク", "ガー"}
NAME_SEPARATOR = "・"


def name_part(word) -> str | None:
    """pos4 of a person-name token (姓, 名, 一般), None for any other token."""
    if word.surface in EXTRA_NAMES:
        return "一般"
    feature = word.feature
    if getattr(feature, "pos2", None) == "固有名詞" and getattr(feature, "pos3", None) == "人名":
        return getattr(feature, "pos4", None)
    return None


def person_key(span: list[tuple[str, str]]) -> str | None:
    """Canonical key of a name span made of (surface, part) tokens."""
    surnames = [surface for surface, part in span if part == "姓"]
    others = [surface for surface, part in span if part == "一般"]
    if surnames:
        key = surnames[0]
    elif others:
        key = others[-1]
    else:
        return None
    if len(key) < 2 or key in NOT_PERSONS:
        return None
    return key


def extract_people(words) -> dict[str, list[str]]:
    """Map each person named in a tagged text to the forms it is written in."""
    people = defaultdict(list)
    span = []

    def flush():
        key = person_key(span)
        if key is not None:
            # Foreign names keep their separator, Japanese ones are written without
            foreign = any(part == "一般" for _, part in span)
            people[key].append((NAME_SEPARATOR if foreign else "").join(s for s, _ in span))
        span.clear()

    words = list(words)
    for i, word in enumerate(words):
        part = name_part(word)
        if part == "姓" and span:
            flush()
        if part is not None:
            span.append((word.surface, part))
        elif (word.surface == NAME_SEPARATOR and span
              and i + 1 < len(words) and name_part(words[i + 1]) is not None):
            continue
        elif span:
            flush()
    if span:
        flush()
    return dict(people)


def ensure_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS person (
                id serial PRIMARY KEY,
                name text UNIQUE NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS post_person (
                post_id integer REFERENCES posts(id) ON DELETE CASCADE,
                person_id integer REFERENCES person(id) ON DELETE CASCADE,
                mentions smallint NOT NULL,
                surface_forms text[] NOT NULL,
                PRIMARY KEY (post_id, person_id)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS post_person_person_id ON post_person (person_id)"
        ))


def save_people(conn, results: dict[int, dict[str, list[str]]]):
    """Replace the people linked to these posts, within the caller's transaction."""
    conn.execute(text("DELETE FROM post_person WHERE post_id = ANY(:ids)"),
                 {"ids": list(results)})
    names = sorted({name for people in results.values() for name in people})
    if not names:
        return
    conn.execute(text("""
        INSERT INTO person (name) SELECT unnest(CAST(:names AS text[]))
        ON CONFLICT (name) DO NOTHING
    """), {"names": names})
    ids = dict(conn.execute(text("SELECT name, id FROM person WHERE name = ANY(:names)"),
                            {"names": names}).all())
    conn.execute(text("""
        INSERT INTO post_person (post_id, person_id, mentions, surface_forms)
        VALUES (:post_id, :person_id, :mentions, :surface_forms)
    """), [
        {
            "post_id": post_id,
            "person_id": ids[name],
            "mentions": min(len(forms), 32767),
            "surface_forms": sorted(set(forms)),
        }
        for post_id, people in results.items()
        for name, forms in people.items()
    ])


def delete_orphans(conn):
    """Remove people no post in the analyzed period mentions anymore."""
    conn.execute(text("""
        DELETE FROM person pe
        WHERE NOT EXISTS (SELECT 1 FROM post_person pp WHERE pp.person_id = pe.id)
    """))
