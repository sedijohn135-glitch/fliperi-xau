# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN useradd --create-home --uid 10001 trader && chown -R trader:trader /app
USER trader

CMD ["python", "-u", "main.py"]
