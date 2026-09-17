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
# (Scrapling/Playwright's own `scrapling install` step below pulls in the
#  rest of the browser system deps it needs via `playwright install --with-deps`;
#  these cover the base tooling plus common headless-Chromium runtime libs.)
# ===============================
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg curl ca-certificates procps \
    fonts-liberation libappindicator3-1 libasound2 libatk-bridge2.0-0 \
    libatk1.0-0 libcups2 libdbus-1-3 libgdk-pixbuf-xlib-2.0-0 \
    libnspr4 libnss3 libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 \
    xdg-utils libgbm1 libxss1 \
    && rm -rf /var/lib/apt/lists/*

# ===============================
# APP SETUP
# ===============================
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ===============================
# INSTALL SCRAPLING'S BROWSERS
# Downloads the Chromium/Patchright build + fingerprint-manipulation deps that
# StealthyFetcher/StealthySession need. Replaces the old manual Google Chrome +
# matching-chromedriver install dance entirely.
# ===============================
RUN scrapling install

COPY . .

# ===============================
# CLEANUP SCRIPT (optional but powerful)
# ===============================
RUN echo '#!/bin/sh\npkill -f chromium || true\npkill -f playwright || true' > /usr/local/bin/cleanup.sh \
    && chmod +x /usr/local/bin/cleanup.sh

# ===============================
# RUN APP
# ===============================
EXPOSE 8080
ENV PORT=8080

CMD ["python", "scraper.py"]
