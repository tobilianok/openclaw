FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY config.yaml ./config.yaml
RUN useradd --uid 1000 --create-home jarvis && mkdir -p /data && chown jarvis /data
USER jarvis
ENV JARVIS_SSH_CONFIG=/app/config.yaml JARVIS_SSH_DATA=/data
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/sante')"
CMD ["uvicorn", "app.service:app_depuis_env", "--factory", "--host", "0.0.0.0", "--port", "8001"]
