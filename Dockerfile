FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD test -f server.py || cp server-1.py server.py; test -f index.html || cp index-1.html index.html; exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}
