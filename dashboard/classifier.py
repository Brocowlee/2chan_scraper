"""Classification of the scraped images, used to filter the mosaics.

The model is a ViT fine-tuned on 5 classes. Scores of every class are stored in
the `image_classifications` table, so the filter threshold can be changed
without classifying again.
"""
import json
import logging
import os
from pathlib import Path

from PIL import Image
from sqlalchemy import text

from app.database import engine
from dashboard.config import START_DATE

logger = logging.getLogger(__name__)

MODEL = "giacomoarienti/nsfw-classifier"
MODEL_REVISION = "29f43cab33874e62db8a1973bd0f84b0c69ff057"
MODEL_ID = f"{MODEL}@{MODEL_REVISION}"
NSFW_LABELS = ("hentai", "porn", "sexy")
# An image is filtered out when the summed probability of these labels reaches this
NSFW_THRESHOLD = float(os.getenv("DASHBOARD_FILTER_THRESHOLD", "0.5"))
BATCH_SIZE = 16
# Leave a core to the other services of the host
TORCH_THREADS = int(os.getenv("DASHBOARD_TORCH_THREADS", "3"))

_model = None


def ensure_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS image_classifications (
                post_id integer PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
                model text NOT NULL,
                label text NOT NULL,
                nsfw_score real NOT NULL,
                scores jsonb NOT NULL,
                classified_at timestamp NOT NULL DEFAULT now()
            )
        """))


def get_model():
    global _model
    if _model is None:
        import torch
        from transformers import AutoImageProcessor, AutoModelForImageClassification

        torch.set_num_threads(TORCH_THREADS)
        processor = AutoImageProcessor.from_pretrained(MODEL, revision=MODEL_REVISION)
        model = AutoModelForImageClassification.from_pretrained(MODEL, revision=MODEL_REVISION).eval()
        _model = (processor, model)
    return _model


def load_image(path: Path) -> Image.Image:
    with Image.open(path) as im:
        # The model works at 224px: no need to decode JPEGs at full size
        im.draft("RGB", (448, 448))
        im.seek(0)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            background = Image.new("RGBA", im.size, "white")
            im = Image.alpha_composite(background, im)
        return im.convert("RGB")


def classify(images: list[Image.Image]) -> list[dict[str, float]]:
    import torch

    processor, model = get_model()
    inputs = processor(images=images, return_tensors="pt")
    with torch.inference_mode():
        probs = model(**inputs).logits.softmax(-1)
    labels = model.config.id2label
    return [{labels[i]: round(float(p), 4) for i, p in enumerate(row)} for row in probs]


def classify_pending(image_dir: Path, extensions: set[str], progress: dict) -> int:
    """Classify the images of the analyzed period that have no classification yet."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT p.id, p.local_image_path
            FROM posts p
            LEFT JOIN image_classifications c ON c.post_id = p.id AND c.model = :model
            WHERE p.posted_at >= :start AND p.local_image_path IS NOT NULL AND c.post_id IS NULL
            ORDER BY p.id
        """), {"model": MODEL_ID, "start": START_DATE}).all()
    pending = [
        (post_id, image_dir / Path(path).name) for post_id, path in rows
        if Path(path).suffix.lower().lstrip(".") in extensions
    ]
    if not pending:
        return 0

    logger.info(f"Classifying {len(pending)} images...")
    progress.update(state="classifying", done=0, total=len(pending))
    for start in range(0, len(pending), BATCH_SIZE):
        batch, images, failed = [], [], []
        for post_id, path in pending[start:start + BATCH_SIZE]:
            try:
                images.append(load_image(path))
                batch.append(post_id)
            except Exception as e:
                logger.warning(f"Cannot classify {path.name}: {e}")
                failed.append(post_id)
        results = classify(images) if images else []
        # Unreadable images are recorded too, so they aren't retried every cycle
        records = [
            {
                "post_id": post_id,
                "model": MODEL_ID,
                "label": max(scores, key=scores.get),
                "nsfw_score": sum(scores[label] for label in NSFW_LABELS),
                "scores": json.dumps(scores),
            }
            for post_id, scores in zip(batch, results)
        ] + [
            {"post_id": post_id, "model": MODEL_ID, "label": "error",
             "nsfw_score": 0, "scores": "{}"}
            for post_id in failed
        ]
        if records:
            with engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO image_classifications (post_id, model, label, nsfw_score, scores)
                    VALUES (:post_id, :model, :label, :nsfw_score, CAST(:scores AS jsonb))
                    ON CONFLICT (post_id) DO UPDATE
                    SET model = EXCLUDED.model, label = EXCLUDED.label,
                        nsfw_score = EXCLUDED.nsfw_score, scores = EXCLUDED.scores,
                        classified_at = now()
                """), records)
        progress["done"] = min(start + BATCH_SIZE, len(pending))
    logger.info(f"Classified {len(pending)} images.")
    return len(pending)
