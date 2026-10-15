FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD S=$(find /app -name server.py | head -1); ls -la /app; echo "server.py em: $S"; cd "$(dirname "$S")" && exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}
