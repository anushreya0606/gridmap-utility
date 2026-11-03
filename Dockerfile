# Grid mapping check website
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and UI assets
COPY *.py ./
COPY templates/ ./templates/
COPY static/ ./static/

# Runs, stored events and the stored MI file live in /data
ENV GRIDMAP_DATA=/data
# 1 = public website: every browser gets its own private workspace (no login), deleted after GRIDMAP_KEEP_DAYS.
# 0 = one shared workspace for a team on a private network.
ENV GRIDMAP_PUBLIC=1 GRIDMAP_KEEP_DAYS=7

VOLUME /data
EXPOSE 10000

# Dynamically bind to the PORT environment variable provided by Render/Cloud platforms
CMD ["sh", "-c", "python app.py --host 0.0.0.0 --port ${PORT:-10000}"]
