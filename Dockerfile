FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
# HF Spaces expects port 7860; Railway sets its own $PORT automatically.
# Defaulting to 7860 means this same Dockerfile works on both platforms
# unchanged.
ENV PORT=7860
EXPOSE 7860

CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}

