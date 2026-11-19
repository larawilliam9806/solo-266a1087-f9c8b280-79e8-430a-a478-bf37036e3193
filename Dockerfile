FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY railapp ./railapp
COPY run.py ./

RUN mkdir -p /data && useradd --system --uid 10001 --home /app rail \
    && chown -R rail:rail /data /app
USER rail

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"

# One worker keeps SQLite write contention predictable; the app's own
# BEGIN IMMEDIATE transactions remain the source of truth if scaled out.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "120", "--graceful-timeout", "30", "--access-logfile", "-", "run:app"]
