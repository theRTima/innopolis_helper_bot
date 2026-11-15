FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt requirements-ingestion.txt ./

RUN pip install -r requirements.txt


RUN useradd --create-home --uid 1000 app
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app rag_pipeline ./rag_pipeline
COPY --chown=app:app alembic.ini pyproject.toml ./

USER app
CMD [ "python", "-m", "app" ]
