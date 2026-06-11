FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies including curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy entire project
COPY . .

# Install Python dependencies from the fully pinned lock file
# (requirements.lock pins all transitive deps; requirements.txt only pins direct deps)
RUN pip3 install --no-cache-dir -r requirements.lock

# Create directories for database and logs
RUN mkdir -p /app/data /app/logs && \
    chmod 777 /app/data /app/logs

# Set environment variables
ENV DATABASE_URL=sqlite:////app/data/rollcall.db
ENV PYTHONUNBUFFERED=1
ENV HEALTH_CHECK_PORT=8080

# Expose health check port
EXPOSE 8080

# Change to the directory where runner.py is located
WORKDIR /app/rollCall

# Run the bot
CMD ["python", "runner.py"]

