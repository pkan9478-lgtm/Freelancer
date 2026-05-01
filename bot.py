import os
import asyncio
import logging
import json
import tempfile
import zipfile
import shutil
import re
import gc
from datetime import datetime

import aiosqlite
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes, 
    CallbackQueryHandler
)
from groq import AsyncGroq
from dotenv import load_dotenv

# --- Configuration & Logging ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None
PORT = int(os.environ.get("PORT", 8000))

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

DB_NAME = 'phogo_ultra.db'
FALLBACK_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

# --- 1. Render Health Check & Web Server ---
# Render မှ Port မတွေ့ပါက Restart ကျတတ်သဖြင့် ဤ Server သည် အရေးကြီးသည်
async def handle_health_check(request):
    return web.Response(text="Bot Instance is Running Smoothly!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"✅ Web Server started on Port {PORT}")

# --- 2. Database Initialization ---
async def init_db(app):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('PRAGMA journal_mode=WAL;')
        await db.execute('''CREATE TABLE IF NOT EXISTS job_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER, job_desc TEXT, project_plan TEXT,
                      tech_stack TEXT, status TEXT)''')
        await db.commit()
    logger.info("✅ SQLite Database Initialized")

# --- 3. AI Helper Class ---
class PhoGoAI:
    def __init__(self):
        self.client = AsyncGroq(api_key=GROQ_API_KEY)

    async def chat_json(self, prompt, sys_msg):
        for model in FALLBACK_MODELS:
            try:
                resp = await self.client.chat.completions.create(
                    model=model,
                    messages=[{"role":"system","content":sys_msg},{"role":"user","content":prompt}],
                    response_format={"type": "json_object"}
                )
                return json.loads(resp.choices[0].message.content)
            except Exception as e:
                logger.error(f"Groq API Error ({model}): {e}")
        return None

ai_helper = PhoGoAI()

# --- 4. Utilities ---
def create_zip(files_dict, job_id):
    tmp_dir = tempfile.mkdtemp()
    zip_name = f"Build_Job_{job_id}.zip"
    zip_path = os.path.join(tempfile.gettempdir(), zip_name)
    try:
        for path, content in files_dict.items():
            full_path = os.path.join(tmp_dir, path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(content)
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for r, d, files in os.walk(tmp_dir):
                for f in files:
                    fp = os.path.join(r, f)
                    zf.write(fp, os.path.relpath(fp, tmp_dir))
        return zip_path
    finally:
        shutil.rmtree(tmp_dir)
        gc.collect()

# --- 5. Bot Commands & Callbacks ---
async def start_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    desc = " ".join(context.args)
    if not desc:
        await update.message.reply_text("❌ အသုံးပြုပုံ- `/job <Project အကြောင်းအရာ>`")
        return
    
    msg = await update.message.reply_text("🧠 AI က Architecture နှင့် Plan ရေးဆွဲနေသည်...")
    
    sys_msg = "Output ONLY JSON. Keys: proposal, price, timeline, tech_stack, project_plan."
    data = await ai_helper.chat_json(f"Requirement: {desc}", sys_msg)
    
    if data:
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute(
                "INSERT INTO job_history (user_id, job_desc, project_plan, tech_stack, status) VALUES (?,?,?,?,?)",
                (ADMIN_ID, desc, data['project_plan'], data['tech_stack'], 'Planning')
            )
            job_id = cur.lastrowid
            await db.commit()
        
        kb = [[InlineKeyboardButton("🚀 Build Full Code (Zip)", callback_data=f"build_{job_id}")]]
        response_text = (
            f"🎯 **Project Plan Ready! (ID: {job_id})**\n\n"
            f"🛠 **Tech Stack:** {data['tech_stack']}\n"
            f"⏳ **Timeline:** {data['timeline']}\n\n"
            "စနစ်တစ်ခုလုံးကို Code ရေးသားပြီး Zip ထုတ်ယူရန် ခလုတ်ကို နှိပ်ပါ။"
        )
        await msg.edit_text(response_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    else:
        await msg.edit_text("❌ AI Response Error. နောက်မှ ပြန်ကြိုးစားပါ။")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("build_"):
        job_id = query.data.split("_")[1]
        msg = await query.message.reply_text("⚙️ Code များရေးသားပြီး Zip ဖိုင် တည်ဆောက်နေသည်...")
        
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute("SELECT project_plan, tech_stack FROM job_history WHERE id=?", (job_id,))
            row = await cur.fetchone()
        
        if row:
            sys_msg = "Create full system code. Output ONLY JSON where keys are file paths and values are code strings."
            code_data = await ai_helper.chat_json(f"Plan: {row[0]}\nTech: {row[1]}", sys_msg)
            
            if code_data:
                zip_p = create_zip(code_data, job_id)
                with open(zip_p, 'rb') as f:
                    await query.message.reply_document(document=f, caption=f"✅ Build Complete (Job: {job_id})")
                os.remove(zip_p)
                await msg.delete()
            else:
                await msg.edit_text("❌ Code Generation Error.")

# --- 6. Main Runner ---
if __name__ == '__main__':
    # ၁။ Web Server ကို စတင်ရန် (Conflict Error ရှောင်ရန် အရေးကြီးသည်)
    loop = asyncio.get_event_loop()
    loop.create_task(start_web_server())
    
    # ၂။ Bot Application တည်ဆောက်ရန်
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(init_db).build()
    
    # Handler များ
    app.add_handler(CommandHandler("job", start_job))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    logger.info("🚀 Bot is starting and dropping old updates...")
    
    # ၃။ Conflict Error မတက်စေရန် drop_pending_updates=True ကို သေချာသုံးပါ
    app.run_polling(drop_pending_updates=True)
