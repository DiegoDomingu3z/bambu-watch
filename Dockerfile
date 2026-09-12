FROM python:3.12-slim

# Pillow needs libjpeg at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libjpeg62-turbo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

# Non-root, per the design. The data volume must be writable by this user.
RUN useradd --create-home --uid 10001 watcher \
    && mkdir -p /app/data/sessions \
    && chown -R watcher:watcher /app/data
USER watcher

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
