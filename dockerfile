FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies including curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    curl \
    fonts-dejavu-core \
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

# Bake in what actually got built, for version_info.log_startup_banner().
# Declared this late on purpose: an ARG busts the cache for every layer from
# its declaration onward, and GIT_SHA/BUILD_DATE change on every commit — put
# above the pip install, they would invalidate it on every single build.
# Down here, only this one negligible RUN re-runs.
#
# Both default to "unknown" so a plain `docker build` (no --build-arg, as a
# developer running locally would do) still produces a working image —
# version_info.py already treats "unknown" as an expected value, not an error.
ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown
RUN printf '{"commit": "%s", "built_at": "%s"}\n' "$GIT_SHA" "$BUILD_DATE" > .build_info.json

# Run the bot
CMD ["python", "runner.py"]

