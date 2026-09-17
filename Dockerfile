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
# Downloads the Chromium/Camoufox build + fingerprint-manipulation deps that
# scrapling's fetchers need. Replaces the old manual Google Chrome +
# matching-chromedriver install dance entirely.
#
# StealthyFetcher/StealthySession specifically run on Patchright (Scrapling's
# stealth engine since v0.3.13), which manages its OWN Chromium build in a
# separate revision from vanilla Playwright's — `scrapling install` alone can
# leave that one missing ("Executable doesn't exist at .../chromium-XXXX"),
# so install it explicitly too. `--force` on both guards against a cached
# ".scrapling_dependencies_installed" marker short-circuiting the download on
# rebuilds.
# ===============================
RUN scrapling install --force \
    && python -m patchright install --with-deps chromium

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
