FROM python:3.11-slim

# Install Chrome + ChromeDriver
RUN apt-get update && apt-get install -y \
    wget gnupg curl unzip \
    fonts-liberation libappindicator3-1 libasound2 libatk-bridge2.0-0 \
    libatk1.0-0 libcups2 libdbus-1-3 libgdk-pixbuf2.0-0 libnspr4 \
    libnss3 libxcomposite1 libxdamage1 libxrandr2 xdg-utils \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y google-chrome-stable --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py .

# Railway persistent volume
RUN mkdir -p /data

EXPOSE 8080
CMD ["python", "scraper.py"]
