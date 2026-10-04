FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src

EXPOSE 8000

# Railway runs migrations first (preDeployCommand in railway.toml), then starts the service.
CMD ["sh", "-c", "uvicorn hrmgmt.main:app_factory --factory --host 0.0.0.0 --port ${PORT:-8000}"]
