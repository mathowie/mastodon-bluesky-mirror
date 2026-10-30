FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Keep build inputs and verification outside the /app source bind mount.
COPY requirements.txt /opt/masto-mirror/requirements.txt
COPY app/core/image_runtime.py /opt/masto-mirror/image_runtime.py
RUN python -m pip install --no-cache-dir -r /opt/masto-mirror/requirements.txt \
    && python /opt/masto-mirror/image_runtime.py

# Application source and durable data are provided by docker-compose.yaml.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).close()"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
