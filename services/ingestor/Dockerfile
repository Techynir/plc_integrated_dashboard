# Build context is the repository root (the JSON Schema lives in /schemas).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY services/ingestor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY schemas ./schemas
COPY services/ingestor/ingestor ./ingestor

RUN useradd --uid 10001 --no-create-home app
USER app

CMD ["python", "-m", "ingestor.main"]
