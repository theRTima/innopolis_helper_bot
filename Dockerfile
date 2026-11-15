FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Copy the project before installing because requirements/base.txt uses -e .
COPY pyproject.toml ./
COPY requirements ./requirements
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
COPY alembic.ini ./

RUN pip install -r requirements/base.txt

RUN useradd --create-home --uid 1000 app \
    && chown -R app:app /app

USER app

CMD ["python", "-m", "app"]
