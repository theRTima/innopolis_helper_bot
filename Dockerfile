# syntax=docker/dockerfile:1.7

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies in a separate layer. The empty stub package keeps
# ``pip install -e .`` (requirements/base.txt) working without copying the real
# sources, so normal code edits do not invalidate the ~200 MB dependency layer.
COPY pyproject.toml ./
COPY requirements ./requirements
RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir -p app \
    && touch app/__init__.py \
    && pip install -r requirements/base.txt \
    && rm -rf app

# Source changes only invalidate these small layers.
COPY --chown=1000:1000 app ./app
COPY --chown=1000:1000 migrations ./migrations
COPY --chown=1000:1000 scripts ./scripts
COPY --chown=1000:1000 alembic.ini ./

RUN useradd --create-home --uid 1000 app

USER app

CMD ["python", "-m", "app"]
