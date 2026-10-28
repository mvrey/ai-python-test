FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Don't run the service as root.
RUN useradd --system --no-create-home app
USER app

EXPOSE 5000

# /health returns 503 when a pipeline worker has died.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:5000/health', timeout=2)"]

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5000"]
