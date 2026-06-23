FROM python:3.12-slim

WORKDIR /app

# Install system dependencies required by MinerU/PDF processing.
# We keep the runtime libraries only; wheels are used for Python packages.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Ensure config/model directories exist even when not mounted
RUN mkdir -p /root/.mineru/models

COPY app.py .

EXPOSE 8002

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002"]
