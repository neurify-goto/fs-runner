# Form Sender Cloud Run Job container

FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    TZ=Asia/Tokyo \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    FORM_SENDER_ENV=cloud_run \
    FORM_SENDER_LOG_SANITIZE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN playwright install --with-deps chromium

ENTRYPOINT ["python", "bin/form_sender_job_entry.py"]
