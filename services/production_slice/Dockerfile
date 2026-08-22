FROM mirror.gcr.io/library/python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system --gid 10001 researchops \
    && useradd --system --uid 10001 --gid researchops --home-dir /app researchops

WORKDIR /app

COPY pyproject.toml /app/core/pyproject.toml
COPY src /app/core/src

COPY services/production_slice/pyproject.toml /app/service/pyproject.toml
COPY services/production_slice/requirements.runtime.lock /app/service/requirements.runtime.lock
COPY services/production_slice/src /app/service/src
RUN python -m pip install -r /app/service/requirements.runtime.lock \
    && python -m pip install --no-deps /app/core \
    && python -m pip install --no-deps /app/service

USER 10001:10001

EXPOSE 8080
CMD ["researchops-api"]
