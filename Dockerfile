# ပေါ့ပါးသော Python Image ကို အသုံးပြုခြင်း
FROM python:3.10-slim

WORKDIR /app

# Cache မကျန်စေရန် --no-cache-dir ဖြင့် Install လုပ်ခြင်း
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000

# 512MB RAM အတွက် Worker (1) ခုတည်းသာ အသုံးပြုရန်
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1"]
