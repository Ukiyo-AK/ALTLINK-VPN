FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY BOT_USAGE.md ADMIN_USAGE.md DEPLOYMENT.md ./

RUN pip install --upgrade pip && pip install .

CMD ["python", "-m", "altlink.main", "backend"]

