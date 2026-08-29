FROM python:3.12-slim

WORKDIR /app

# Install git (needed to pip-install pyquotex from GitHub)
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (pyquotex git URL is already inside requirements.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Dashboard port
EXPOSE 5000

# Environment variables — set these in Railway:
#   QUOTEX_EMAIL       your Quotex account email
#   QUOTEX_PASSWORD    your Quotex account password
# Telegram credentials are stored in quotex_bot_session.json (persist via volume)

CMD ["python", "main.py"]
