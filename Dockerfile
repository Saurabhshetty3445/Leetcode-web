# Pin to Bookworm (Debian 12) — stable package names, no Trixie renames
FROM python:3.11-slim-bookworm

# ── System dependencies ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip gnupg ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libgdk-pixbuf2.0-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libxss1 \
    libgbm1 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# ── Install Chrome for Testing (matched stable pair) ─────────────────────────
# Fetches both chrome + chromedriver at the exact same version via the
# official JSON API — no apt-key, no stale CDN, always in sync.
RUN set -ex \
    && CHROME_JSON=$(curl -sSf \
        "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json") \
    && CHROME_URL=$(echo "$CHROME_JSON" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); \
         print([x['url'] for x in d['channels']['Stable']['downloads']['chrome'] \
                if x['platform']=='linux64'][0])") \
    && DRIVER_URL=$(echo "$CHROME_JSON" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); \
         print([x['url'] for x in d['channels']['Stable']['downloads']['chromedriver'] \
                if x['platform']=='linux64'][0])") \
    && echo "Chrome  : $CHROME_URL" \
    && echo "Driver  : $DRIVER_URL" \
    \
    && curl -sSL "$CHROME_URL"  -o /tmp/chrome.zip \
    && unzip -q /tmp/chrome.zip  -d /opt/ \
    && mv /opt/chrome-linux64   /opt/chrome \
    && ln -sf /opt/chrome/chrome /usr/local/bin/google-chrome \
    && chmod +x /opt/chrome/chrome \
    \
    && curl -sSL "$DRIVER_URL" -o /tmp/chromedriver.zip \
    && unzip -q /tmp/chromedriver.zip -d /opt/ \
    && mv /opt/chromedriver-linux64    /opt/chromedriver \
    && ln -sf /opt/chromedriver/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /opt/chromedriver/chromedriver \
    \
    && rm -f /tmp/chrome.zip /tmp/chromedriver.zip \
    && google-chrome --version \
    && chromedriver --version

# ── Python app ────────────────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
ENV PORT=8080
ENV CHROME_BIN=/opt/chrome/chrome
ENV CHROMEDRIVER_BIN=/opt/chromedriver/chromedriver

CMD ["python", "scraper.py"]
