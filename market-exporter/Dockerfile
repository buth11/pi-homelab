FROM python:3.12-slim

# No .pyc files: lets the pod run with a read-only root filesystem.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY exporter.py .

# Non-root, numeric UID so Kubernetes runAsNonRoot can verify it.
USER 10001
EXPOSE 8000
CMD ["python", "exporter.py"]
