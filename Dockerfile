FROM python:3.12-slim

WORKDIR /srv

# System deps for cryptography wheels are prebuilt; keep image small
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/
COPY wsgi.py .

# Data dir for sqlite + encryption key (mount a volume here)
RUN mkdir -p /data
ENV AIMI_DB=/data/aimi.db
ENV AIMI_KEY=/data/secret.key
ENV PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8080/health || exit 1

# 2 workers is plenty for personal/family self-hosting
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "120", "wsgi:app"]
