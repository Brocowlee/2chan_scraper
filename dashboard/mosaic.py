"""Mosaics of the images gathered by the scraper, each rendered as a single JPEG.

Images are first classified by the image classifier, which splits them into two
sets: the "safe" one and the filtered one (the latter exists for checking the
filter). Each image
is reduced once to a square thumbnail (cached on disk), then the thumbnails of
a set are pasted in a grid, in two layouts: chronological, and sorted by
average color. A JSON index maps every tile back to its post.
"""
import colorsys
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps
from sqlalchemy import text

from app.database import engine
from dashboard import classifier
from dashboard.config import START_DATE

logger = logging.getLogger(__name__)

IMAGE_DIR = Path(os.getenv("DASHBOARD_IMAGE_DIR", "/srv/images"))
CACHE_DIR = Path(os.getenv("DASHBOARD_CACHE_DIR", "/srv/cache"))
THUMB_DIR = CACHE_DIR / "thumbs"
INDEX_FILE = CACHE_DIR / "mosaic.json"

TILE = 48
# Width / height ratio of the mosaic
ASPECT = 1.5
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}
LAYOUTS = ("time", "color")
SETS = ("safe", "nsfw")
REBUILD_SECONDS = 30 * 60
WORKERS = 4

status = {"state": "idle", "done": 0, "total": 0}


def mosaic_file(set_name: str, layout: str) -> Path:
    return CACHE_DIR / f"mosaic-{set_name}-{layout}.jpg"


def load_index() -> dict | None:
    try:
        index = json.loads(INDEX_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    # Ignore indexes written before the split into sets
    return index if "sets" in index else None


def make_thumbnail(post_number: int, filename: str) -> Path | None:
    """Return the cached thumbnail of an image, creating it if needed."""
    thumb = THUMB_DIR / f"{post_number}.jpg"
    failed = THUMB_DIR / f"{post_number}.fail"
    if thumb.exists():
        return thumb
    if failed.exists():
        return None
    try:
        with Image.open(IMAGE_DIR / filename) as im:
            # Lets the JPEG decoder skip most of the full-size decoding
            im.draft("RGB", (TILE * 4, TILE * 4))
            im.seek(0)
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                background = Image.new("RGBA", im.size, "white")
                im = Image.alpha_composite(background, im)
            im = ImageOps.fit(im.convert("RGB"), (TILE, TILE), Image.LANCZOS)
            im.save(thumb, "JPEG", quality=85)
        return thumb
    except Exception as e:
        logger.warning(f"Cannot make thumbnail of {filename}: {e}")
        failed.touch()
        return None


def color_key(rgb: tuple[int, int, int]) -> tuple:
    """Grays first (dark to light), then colors by hue then lightness."""
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    if s < 0.15 or v < 0.12:
        return (0, 0, v)
    return (1, int(h * 18), v)


def write_atomic(path: Path, write):
    tmp = path.with_suffix(path.suffix + ".tmp")
    write(tmp)
    os.replace(tmp, path)


def render(set_name: str, entries: list[tuple]) -> dict:
    """Write the mosaics of a set of (item, color, tile) and return its index."""
    count = len(entries)
    cols = max(1, math.ceil(math.sqrt(count * ASPECT)))
    rows_count = max(1, math.ceil(count / cols))
    layouts = {
        "time": list(range(count)),
        "color": sorted(range(count), key=lambda i: color_key(entries[i][1])),
    }
    for layout, order in layouts.items():
        canvas = Image.new("RGB", (cols * TILE, rows_count * TILE), "black")
        for position, i in enumerate(order):
            canvas.paste(entries[i][2], ((position % cols) * TILE, (position // cols) * TILE))
        write_atomic(
            mosaic_file(set_name, layout),
            lambda tmp: canvas.save(tmp, "JPEG", quality=82, optimize=True, progressive=True),
        )
    return {
        "cols": cols,
        "rows": rows_count,
        "count": count,
        "items": [item for item, _, _ in entries],
        "layouts": layouts,
    }


def build(force: bool = False):
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    classifier.ensure_table()
    classifier.classify_pending(IMAGE_DIR, IMAGE_EXTENSIONS, status)

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT p.post_number, t.thread_number, p.posted_at, p.local_image_path,
                   c.label, c.nsfw_score
            FROM posts p
            JOIN threads t ON t.id = p.thread_id
            LEFT JOIN image_classifications c ON c.post_id = p.id AND c.model = :model
            WHERE p.posted_at >= :start AND p.local_image_path IS NOT NULL
            ORDER BY p.posted_at, p.post_number
        """), {"start": START_DATE, "model": classifier.MODEL_ID}).all()

    classified = sum(row.label is not None for row in rows)
    signature = [len(rows), rows[-1].post_number if rows else 0, classified,
                 TILE, classifier.MODEL_ID, classifier.NSFW_THRESHOLD]
    index = load_index()
    if not force and index and index.get("signature") == signature \
            and all(mosaic_file(s, l).exists() for s in SETS for l in LAYOUTS):
        return

    images = [
        (row, Path(row.local_image_path).name) for row in rows
        if Path(row.local_image_path).suffix.lower().lstrip(".") in IMAGE_EXTENSIONS
    ]
    status.update(state="building", done=0, total=len(images))
    logger.info(f"Building mosaics from {len(images)} images...")

    def thumbnail(item):
        row, filename = item
        path = make_thumbnail(row.post_number, filename)
        status["done"] += 1
        return path

    with ThreadPoolExecutor(WORKERS) as pool:
        thumbs = list(pool.map(thumbnail, images))

    sets = {set_name: [] for set_name in SETS}
    unclassified = 0
    for (row, filename), thumb in zip(images, thumbs):
        if thumb is None:
            continue
        # Fail closed: an image that wasn't classified never reaches the safe mosaic
        if row.label is None or row.label == "error":
            unclassified += 1
            continue
        with Image.open(thumb) as im:
            tile = im.convert("RGB")
        item = [row.post_number, row.thread_number, row.posted_at.isoformat(), filename,
                row.label, round(row.nsfw_score, 3)]
        color = tile.resize((1, 1), Image.BOX).getpixel((0, 0))
        set_name = "nsfw" if row.nsfw_score >= classifier.NSFW_THRESHOLD else "safe"
        sets[set_name].append((item, color, tile))

    index = {
        "signature": signature,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tile": TILE,
        "threshold": classifier.NSFW_THRESHOLD,
        "skipped": len(rows) - sum(len(entries) for entries in sets.values()) - unclassified,
        "unclassified": unclassified,
        "sets": {set_name: render(set_name, entries) for set_name, entries in sets.items()},
    }
    write_atomic(INDEX_FILE, lambda tmp: tmp.write_text(json.dumps(index)))
    # Mosaics written before the split into sets
    for layout in LAYOUTS:
        (CACHE_DIR / f"mosaic-{layout}.jpg").unlink(missing_ok=True)
    logger.info("Mosaics built: " + ", ".join(
        f"{name} {data['count']} tiles" for name, data in index["sets"].items()))


def _worker():
    while True:
        try:
            build()
        except Exception as e:
            logger.error(f"Mosaic build failed: {e}")
        finally:
            status["state"] = "idle"
        time.sleep(REBUILD_SECONDS)


def start_worker():
    threading.Thread(target=_worker, name="mosaic", daemon=True).start()
