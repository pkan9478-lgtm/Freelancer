# Base image 
FROM python:3.11-slim-bullseye

# Set environment variables (Python output buffer & bytecode)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Working directory
WORKDIR /app

# Install system dependencies (required for sqlite and minimal ops)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the code
COPY . .

# Run the bot
CMD ["python", "bot.py"]
