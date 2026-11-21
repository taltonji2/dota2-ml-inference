# One image for the API, the dashboard and the ingest CLI.
FROM python:3.12-slim AS build
# EXTRAS=replay adds gem-dota for local replay parsing (image grows from about 205 MB to about 490 MB).
# Build with EXTRAS= for an API-only image, e.g. for Lambda.
ARG EXTRAS=replay
WORKDIR /src
COPY pyproject.toml ./
COPY dota ./dota
COPY app ./app
RUN pip install --no-cache-dir --prefix=/install ".${EXTRAS:+[$EXTRAS]}"

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DOTA_DB=/app/data/dota.sqlite DOTA_REPLAY_DB=/app/data/replays.sqlite
RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY --from=build /install /usr/local
COPY scripts ./scripts
RUN mkdir -p /app/data /app/ml /app/reports && chown -R app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
