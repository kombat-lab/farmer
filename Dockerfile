FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FOG_DATA_DIR=/app/data

WORKDIR /app

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock

RUN adduser --disabled-password --gecos "" farmer \
    && mkdir -p /app/data \
    && chown -R farmer:farmer /app

COPY --chown=farmer:farmer *.py ./

USER farmer
VOLUME ["/app/data"]

CMD ["python", "main.py"]
