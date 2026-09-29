FROM python:3.12-slim-bookworm
WORKDIR /app
COPY handoff /app/handoff
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HANDOFF_HOST=0.0.0.0 HANDOFF_DB=/state/handoff.sqlite3
CMD ["python", "-m", "handoff.server"]
