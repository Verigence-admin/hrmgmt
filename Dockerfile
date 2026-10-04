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

# Migrate, then serve. If the migration fails the container exits and the deploy fails visibly;
# the service never runs against a half-built schema. Alembic is idempotent on restarts.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn hrmgmt.main:app_factory --factory --host 0.0.0.0 --port ${PORT:-8000}"]
