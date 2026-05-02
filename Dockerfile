version: '3.8'

services:
  web:
    build: .
    ports:
      - "8000:8000"
    environment:
      - BOT_TOKEN=YOUR_BOT_TOKEN_HERE
      - WEBAPP_URL=https://your-app.com
      - ADMIN_TELEGRAM_ID=YOUR_TELEGRAM_ID
      - GROQ_API_KEY=YOUR_GROQ_API_KEY
      - REDIS_URL=redis://redis:6379/0
    depends_on:
      - redis

  redis:
    image: redis:alpine
    # Redis ကို RAM 50MB သာ သုံးရန် ကန့်သတ်ထားသည်
    command: redis-server --maxmemory 50mb --maxmemory-policy allkeys-lru
    ports:
      - "6379:6379"
