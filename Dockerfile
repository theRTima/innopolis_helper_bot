FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .

RUN pip install -r requirements.txt


RUN useradd --create-home --uid 1000 app
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app prompts ./prompts
COPY --chown=app:app alembic.ini pyproject.toml vision_parser.py docling_parser.py ./

USER app
CMD [ "python", "-m", "app" ]
