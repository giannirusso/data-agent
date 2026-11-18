FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt pyproject.toml ./
COPY src ./src
COPY config ./config
COPY golden_bucket ./golden_bucket
COPY evals ./evals
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "agent.cli"]
