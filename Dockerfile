# Use stable slim base
FROM python:3.11-slim-bookworm

# ── Install system dependencies (FULL Chrome support) ───────────────────────
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
    libxkbcommon0 \
    xdg-utils \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libatspi2.0-0 \
    libxshmfence1 \
    libx11-6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# ── Install Chrome + Chromedriver (auto-matched versions) ────────────────────
RUN set -ex \
    && CHROME_JSON=$(curl -sSf \
        "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json") \
    \
    && CHROME_URL=$(echo "$CHROME_JSON" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); \
        print([x['url'] for x in d['channels']['Stable']['downloads']['chrome'] \
        if x['platform']=='linux64'][0])") \
    \
    && DRIVER_URL=$(echo "$CHROME_JSON" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); \
        print([x['url'] for x in d['channels']['Stable']['downloads']['chromedriver'] \
        if x['platform']=='linux64'][0])") \
    \
    && echo "Chrome  : $CHROME_URL" \
    && echo "Driver  : $DRIVER_URL" \
    \
    && curl -sSL "$CHROME_URL" -o /tmp/chrome.zip \
    && unzip -q /tmp/chrome.zip -d /opt/ \
    && mv /opt/chrome-linux64 /opt/chrome \
    && ln -sf /opt/chrome/chrome /usr/local/bin/google-chrome \
    && chmod +x /opt/chrome/chrome \
    \
    && curl -sSL "$DRIVER_URL" -o /tmp/chromedriver.zip \
    && unzip -q /tmp/chromedriver.zip -d /opt/ \
    && mv /opt/chromedriver-linux64 /opt/chromedriver \
    && ln -sf /opt/chromedriver/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /opt/chromedriver/chromedriver \
    \
    && rm -rf /tmp/chrome.zip /tmp/chromedriver.zip \
    \
    && google-chrome --version \
    && chromedriver --version

# ── Python app setup ─────────────────────────────────────────────────────────
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ── Environment ──────────────────────────────────────────────────────────────
ENV PORT=8080
ENV CHROME_BIN=/opt/chrome/chrome
ENV CHROMEDRIVER_BIN=/opt/chromedriver/chromedriver

EXPOSE 8080

# ── Start app ────────────────────────────────────────────────────────────────
CMD ["python", "scraper.py"]
