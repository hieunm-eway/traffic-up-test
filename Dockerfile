# TODAYDATE=$(date +%Y%m%d) && docker buildx build --platform=linux/amd64 -t anhndtmo/mo:traffic-up-base-vibing-$TODAYDATE .
# Pin amd64: Chrome for Testing (Google) KHÔNG có bản linux-arm64, và prod chạy amd64.
# Trên Mac M1/M2 (arm64), build mặc định ra arm64 → Chrome không chạy + psutil thiếu
# wheel aarch64 phải biên dịch (cần gcc). Ép amd64 để khớp môi trường cũ (Intel) & prod.
# OrbStack/Docker Desktop sẽ emulate amd64 qua Rosetta.
FROM python:3.12-slim

# Chrome runtime dependencies + utilities cần để tải/giải nén Chrome for Testing
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    unzip \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    libu2f-udev \
    libvulkan1 \
    xdg-utils \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install Chrome for Testing 120 (binary official từ Google, không auto-update)
ARG CHROME_VERSION=120.0.6099.109
RUN wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chrome-linux64.zip" -O /tmp/chrome.zip \
    && unzip -q /tmp/chrome.zip -d /opt \
    && mv /opt/chrome-linux64 /opt/chrome \
    && rm /tmp/chrome.zip

WORKDIR /app

# Copy requirements riêng trước để cache layer pip
COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . ./

# Trỏ uc tới Chrome 120 đã cài + pin version
ENV CHROME_BINARY=/opt/chrome/chrome
ENV CHROME_VERSION=120
ENV HEADLESS=1

ENTRYPOINT ["python3", "main.py"]
