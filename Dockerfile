# syntax=docker/dockerfile:1.6

############################
# 1) Builder: cria venv e instala deps
############################
FROM python:3.13-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# (Opcional) deps de build — geralmente boto3/Flask não precisam, mas é seguro
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Se você tiver requirements.txt, copie e instale
# (Recomendado criar requirements.txt com: Flask, gunicorn, boto3, python-dotenv)
COPY requirements.txt ./

RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip setuptools wheel && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


############################
# 2) Runtime: imagem final enxuta
############################
FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY . .

# Usuário não-root
RUN addgroup --system app && adduser --system --ingroup app app && \
    chown -R app:app /app

USER app

EXPOSE 8005

# Gunicorn só para expor /health; o worker roda em thread ao importar o app
CMD ["gunicorn", "--bind", "0.0.0.0:8005", "app:app"]