FROM mcr.microsoft.com/playwright/python:v1.43.0-jammy

WORKDIR /app

# 1. Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2. Copy the scraper application code
COPY app.py .

# 3. Explicitly default to port 8080 for Google Cloud Run
ENV PORT=8080
EXPOSE 8080

# 4. Start the application
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]