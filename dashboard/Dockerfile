FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY dashboard/requirements.txt /srv/dashboard/requirements.txt
RUN pip install -r /srv/dashboard/requirements.txt

# The dashboard reuses the scraper's database module
COPY app /srv/app
COPY dashboard /srv/dashboard

EXPOSE 8000
CMD ["uvicorn", "dashboard.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
