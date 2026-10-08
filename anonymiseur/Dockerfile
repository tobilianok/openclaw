FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir \
    https://github.com/explosion/spacy-models/releases/download/fr_core_news_lg-3.8.0/fr_core_news_lg-3.8.0-py3-none-any.whl

COPY app ./app
COPY config.yaml ./config.yaml

RUN useradd --uid 1000 --create-home anonymiseur && mkdir -p /data /config && chown anonymiseur /data
USER anonymiseur

ENV ANONYMISEUR_CONFIG=/app/config.yaml
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/sante')"
CMD ["uvicorn", "app.proxy:app_depuis_env", "--factory", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
