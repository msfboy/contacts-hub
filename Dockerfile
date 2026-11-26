# ContactsHub SaaS — Image Docker (Python 3.12 + Node.js 20 pour wa-bridge)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONTACTSHUB_SAAS_MODE=1 \
    CONTACTSHUB_HTTPS=1 \
    CONTACTSHUB_DATA_DIR=/data

# Installer Node.js 20 LTS (pour wa-bridge) + certificats CA
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Dépendances Node.js du pont WhatsApp
COPY wa-bridge/package.json wa-bridge/package-lock.json ./wa-bridge/
RUN cd wa-bridge && npm ci --omit=dev

# Code applicatif
COPY . .

# Volume persistant pour SQLite + backups + session WhatsApp
RUN mkdir -p /data

EXPOSE 8080

# 1 worker Gunicorn + 4 threads (indispensable pour SQLite WAL + schedulers uniques)
CMD ["sh", "-c", "gunicorn app:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT:-8080}"]
