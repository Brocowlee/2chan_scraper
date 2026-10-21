FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY dashboard/requirements.txt /srv/dashboard/requirements.txt
RUN pip install -r /srv/dashboard/requirements.txt

# Bake the image classifier into the image (revision must match dashboard/classifier.py)
ENV HF_HOME=/srv/hf
ARG MODEL_REVISION=29f43cab33874e62db8a1973bd0f84b0c69ff057
RUN python -c "from huggingface_hub import snapshot_download; \
snapshot_download('giacomoarienti/nsfw-classifier', revision='${MODEL_REVISION}')"
ENV HF_HUB_OFFLINE=1

# The dashboard reuses the scraper's database module
COPY app /srv/app
COPY dashboard /srv/dashboard

EXPOSE 8000
CMD ["uvicorn", "dashboard.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
