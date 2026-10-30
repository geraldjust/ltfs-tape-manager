FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends mt-st sg3-utils psmisc && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py archive_catalog.py archive_registration.py archive_scan.py archive_sync.py tar_planning.py tar_runtime.py ./
COPY static ./static
EXPOSE 8080
CMD ["python","app.py"]
