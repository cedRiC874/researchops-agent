FROM mirror.gcr.io/library/python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system --gid 10002 pilot \
    && useradd --system --uid 10002 --gid pilot --home-dir /app pilot

WORKDIR /app

COPY services/production_slice/requirements.runtime.lock /app/locks/base.lock
COPY services/pilot_staging/requirements.provider.lock /app/locks/provider.lock
RUN python -m pip install -r /app/locks/base.lock \
    && python -m pip install -r /app/locks/provider.lock \
    && python -m pip check

COPY pyproject.toml requirements.lock /app/core/
COPY src /app/core/src
COPY evals/v2 /app/core/evals/v2
COPY docs/EXTERNAL_RESEARCHER_PILOT_PROTOCOL.md /app/core/docs/EXTERNAL_RESEARCHER_PILOT_PROTOCOL.md

COPY services/pilot_staging/pyproject.toml /app/pilot/pyproject.toml
COPY services/pilot_staging/src /app/pilot/src
COPY services/pilot_staging/migrations /app/pilot/migrations
COPY services/pilot_staging/content /app/pilot/content
COPY services/pilot_staging/content /app/core/services/pilot_staging/content
COPY services/pilot_staging/contracts /app/core/services/pilot_staging/contracts

RUN python -m pip install --no-deps /app/core \
    && python -m pip install --no-deps /app/pilot

ENV MPLCONFIGDIR=/tmp/matplotlib

USER 10002:10002

EXPOSE 8090
CMD ["researchops-pilot-api"]
