FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SHOPFLOW_HOME=/app

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Generate the source data and run the full pipeline; mount /app/reports to keep the output.
CMD ["sh", "-c", "shopflow generate && shopflow run"]
