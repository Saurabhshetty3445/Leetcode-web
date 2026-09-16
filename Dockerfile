# ===============================
# BASE IMAGE
# ===============================
FROM python:3.11-slim

# ===============================
# ENV (avoid prompts)
# ===============================
ENV DEBIAN_FRONTEND=noninteractive

# ===============================
# SYSTEM DEPENDENCIES
# ===============================
# No browser is installed here anymore — page rendering happens via
# Cloudflare's managed Browser Rendering API (see cloudflare_browser_client.py),
# so the image no longer needs Chrome, chromedriver, or their large set of
# supporting shared libraries. This also removes an entire class of local
# failure modes (Chrome OOM kills, zombie processes, exhausted file
# descriptors) that used to require a container redeploy to recover from.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ===============================
# APP SETUP
# ===============================
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ===============================
# RUN APP
# ===============================
EXPOSE 8080
ENV PORT=8080

CMD ["python", "scraper.py"]
