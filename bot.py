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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.constants import ChatAction
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

# --- Configurations ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None
PORT = int(os.environ.get("PORT", 8000))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_NAME = 'phogo_master.db'
FALLBACK_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
admin_states = {}

# --- 1. Render Health Check Server ---
async def handle_health_check(request):
    return web.Response(text="Bot is Alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

# --- 2. Database Initialization ---
async def init_db(app):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('PRAGMA journal_mode=WAL;')
        await db.execute('''CREATE TABLE IF NOT EXISTS job_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER, job_desc TEXT, project_plan TEXT,
                      tech_stack TEXT, generated_code TEXT, status TEXT)''')
        await db.commit()

# --- 3. AI Logic ---
class PhoGoAI:
    def __init__(self):
        self.client = AsyncGroq(api_key=GROQ_API_KEY)

    async def chat(self, prompt, sys_msg, json_mode=False):
        for model in FALLBACK_MODELS:
            try:
                resp = await self.client.chat.completions.create(
                    model=model,
                    messages=[{"role":"system","content":sys_msg},{"role":"user","content":prompt}],
                    response_format={"type": "json_object"} if json_mode else None
                )
                return resp.choices[0].message.content
            except Exception as e:
                logger.error(f"AI Error: {e}")
        return None

ai = PhoGoAI()

# --- 4. Helpers ---
def create_zip(files_dict, job_id):
    tmp_dir = tempfile.mkdtemp()
    zip_name = f"Build_Job_{job_id}.zip"
    zip_path = os.path.join(tempfile.gettempdir(), zip_name)
    try:
        for path, content in files_dict.items():
            full_path = os.path.join(tmp_dir, path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'w', encoding='utf-8') as f: f.write(content)
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for r, d, files in os.walk(tmp_dir):
                for f in files:
                    fp = os.path.join(r, f)
                    zf.write(fp, os.path.relpath(fp, tmp_dir))
        return zip_path
    finally:
        shutil.rmtree(tmp_dir)
        gc.collect()

# --- 5. Bot Handlers ---
async def start_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    desc = " ".join(context.args)
    if not desc: return await update.message.reply_text("❌ `/job <အကြောင်းအရာ>` ရိုက်ပါ။")
    
    msg = await update.message.reply_text("🧠 Architecture ရေးဆွဲနေသည်...")
    sys_msg = "Output ONLY JSON with keys: proposal, price, timeline, tech_stack, project_plan."
    res = await ai.chat(f"Requirement: {desc}", sys_msg, json_mode=True)
    
    if res:
        data = json.loads(res)
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute("INSERT INTO job_history (user_id, job_desc, project_plan, tech_stack, status) VALUES (?,?,?,?,?)",
                                   (ADMIN_ID, desc, data['project_plan'], data['tech_stack'], 'Planning'))
            job_id = cur.lastrowid
            await db.commit()
        
        kb = [[InlineKeyboardButton("🚀 Build Full Code (Zip)", callback_data=f"build_{job_id}")]]
        await msg.edit_text(f"✅ Architecture Ready (ID: {job_id})\nTech: {data['tech_stack']}", reply_markup=InlineKeyboardMarkup(kb))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("build_"):
        job_id = data.split("_")[1]
        msg = await query.message.reply_text("⚙️ Code များရေးသားနေသည် (Zip ထုတ်ပေးပါမည်)...")
        
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute("SELECT project_plan, tech_stack FROM job_history WHERE id=?", (job_id,))
            row = await cur.fetchone()
        
        sys_msg = f"Create full system. Output ONLY JSON where keys are file paths and values are code strings."
        res = await ai.chat(f"Plan: {row[0]} \nStack: {row[1]}", sys_msg, json_mode=True)
        
        if res:
            files = json.loads(res)
            zip_p = create_zip(files, job_id)
            with open(zip_p, 'rb') as f:
                await query.message.reply_document(document=f, caption=f"✅ Build Complete (Job: {job_id})")
            os.remove(zip_p)

# --- Execution ---
if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.create_task(start_web_server())
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(init_db).build()
    app.add_handler(CommandHandler("job", start_job))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    logger.info("Bot is running...")
    app.run_polling()
