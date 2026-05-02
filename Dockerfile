# Base image အဖြစ် Python 3.11 ကို အသုံးပြုပါမည်
FROM python:3.11-slim

# Working directory သတ်မှတ်ခြင်း
WORKDIR /app

# Requirements ဖိုင်ကို အရင် Copy ကူးပြီး Install လုပ်ခြင်း (Build ပိုမြန်စေရန်)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ကျန်တဲ့ Code အားလုံးကို Copy ကူးခြင်း
COPY . .

# SQLite Database သိမ်းရန် Folder တည်ဆောက်ခြင်း
RUN mkdir -p /app/data

# Render မှ ချပေးမည့် Port
EXPOSE 8000

# စနစ်ကို စတင် Run မည့် Command
CMD ["python", "main.py"]
