FROM python:3.11-slim

ARG UV_VERSION=0.10.8

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /workspace

COPY pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}" \
    && uv export --frozen --no-emit-project --format requirements-txt --output-file /tmp/requirements.lock \
    && python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && python -m pip uninstall --yes uv \
    && rm /tmp/requirements.lock

COPY . .

CMD ["python", "scripts/reproduce.py", "--help"]
