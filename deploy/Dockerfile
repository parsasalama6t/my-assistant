FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ASSISTANT_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY assistant ./assistant
RUN pip install ".[google]" tzdata

VOLUME ["/data"]
EXPOSE 8081
CMD ["my-assistant", "daemon"]
