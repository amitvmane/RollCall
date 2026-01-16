FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies (if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY Requeriments.txt .

# Install Python dependencies
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories for database and logs with proper permissions
RUN mkdir -p /app/data /app/logs && \
    chmod 777 /app/data /app/logs

# Set environment variables for database path
ENV DATABASE_URL=sqlite:////app/data/rollcall.db
ENV PYTHONUNBUFFERED=1

# Run the bot
CMD ["python", "runner.py"]

