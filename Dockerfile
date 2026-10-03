FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project
COPY . .

# Render injects $PORT (defaults to 10000)
ENV PORT=10000

EXPOSE 10000

CMD gunicorn --bind 0.0.0.0:$PORT --timeout 300 --workers 1 --threads 4 app:app