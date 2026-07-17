FROM mcr.microsoft.com/playwright/python:v1.43.0-jammy

WORKDIR /app

# 1. Copy and install dependencies first (caches this step for speed)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2. Copy the scraper application code
COPY app.py .

# 3. Set a default port (Railway will override this automatically)
ENV PORT=7860
EXPOSE 7860

# 4. The ONE and ONLY command to start your application
<<<<<<< HEAD
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
=======
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
>>>>>>> 1b3e194310a94a66e6df180cf404e5d1475ac02f
