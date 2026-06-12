FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies required by asyncpg and psycopg2
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements 
# reinstalls packages when requirements.txt actually changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/

# Not running as root for security: create a non-privileged user
RUN adduser --disabled-password --gecos "" appuser
USER appuser

# Expose port
EXPOSE 8000

# Start the API
# Workers=1 keeps connection pool predictable; scale via ECS task count instead
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]