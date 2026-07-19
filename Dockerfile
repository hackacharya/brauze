FROM python:3.14.6-slim

LABEL version="1.0"  \
      description="Brauze - Yet Another Folder Browser - BRAUSE_ROOT, BRAUZE_ROOT, BRAUZE_WORKSPACE_HEADER, BRAUZE_USERID_HEADER, Put this behind a auth reverse proxy if you want auth" \
      vendor="Hackacharya"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEBFOLDER_ROOT=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gunicorn.conf.py ./gunicorn.conf.py
COPY app.py ./app.py
COPY static ./static
COPY templates ./templates

EXPOSE 8000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "--log-level", "critical", "--access-logfile", "/dev/null", "--error-logfile", "/dev/null", "--bind", "0.0.0.0:8000", "app:app"]
