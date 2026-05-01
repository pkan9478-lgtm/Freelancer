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

# --- Config ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None
PORT = int(os.environ.get("PORT", 8000))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_NAME = 'phogo_ultra.db'
FALLBACK_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

# --- Web Server for Render ---
async def handle_health_check(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()

# --- DB Setup ---
async def init_db(app):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('PRAGMA journal_mode=WAL;')
        await db.execute('''CREATE TABLE IF NOT EXISTS job_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER, job_desc TEXT, project_plan TEXT,
                      tech_stack TEXT, status TEXT)''')
        await db.commit()

# --- AI Helper ---
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
                logger.error(f"AI Error: {e}")
        return None

ai_helper = PhoGoAI()

# --- Bot Handlers ---
async def start_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    desc = " ".join(context.args)
    if not desc:
        await update.message.reply_text("❌ `/job <Project description>`")
        return
    
    msg = await update.message.reply_text("🧠 AI က Architecture ရေးဆွဲနေသည်...")
    
    sys_msg = "Output ONLY JSON. Keys: proposal, price, timeline, tech_stack, project_plan."
    data = await ai_helper.chat_json(f"Requirement: {desc}", sys_msg)
    
    if data:
        # ERROR FIX: project_plan (list/dict) ကို string အဖြစ်ပြောင်းပြီးမှ သိမ်းရပါမည်
        plan_str = json.dumps(data.get('project_plan', 'No plan'))
        stack_str = str(data.get('tech_stack', 'N/A'))

        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute(
                "INSERT INTO job_history (user_id, job_desc, project_plan, tech_stack, status) VALUES (?,?,?,?,?)",
                (ADMIN_ID, desc, plan_str, stack_str, 'Planning')
            )
            job_id = cur.lastrowid
            await db.commit()
        
        # ERROR FIX: callback_data ဟု ပြောင်းလဲထားသည်
        kb = [[InlineKeyboardButton("🚀 Build Full Code (Zip)", callback_data=f"build_{job_id}")]]
        await msg.edit_text(f"✅ Plan Ready (ID: {job_id})\nTech: {stack_str}", 
                            reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg.edit_text("❌ AI Error ဖြစ်သွားပါသည်။")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("build_"):
        job_id = query.data.split("_")[1]
        msg = await query.message.reply_text("⚙️ Code ရေးသားနေသည်...")
        
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute("SELECT project_plan, tech_stack FROM job_history WHERE id=?", (job_id,))
            row = await cur.fetchone()
        
        if row:
            sys_msg = "Create full code. Output ONLY JSON: { 'path/file.py': 'code content' }"
            code_data = await ai_helper.chat_json(f"Plan: {row[0]}\nTech: {row[1]}", sys_msg)
            
            if code_data:
                # Zip ဖန်တီးခြင်း (ယခင် code အတိုင်း)
                tmp = tempfile.mkdtemp()
                z_path = os.path.join(tempfile.gettempdir(), f"Build_{job_id}.zip")
                for p, c in code_data.items():
                    fp = os.path.join(tmp, p)
                    os.makedirs(os.path.dirname(fp), exist_ok=True)
                    with open(fp, 'w') as f: f.write(c)
                with zipfile.ZipFile(z_path, 'w') as z:
                    for r, _, fs in os.walk(tmp):
                        for f in fs: z.write(os.path.join(r, f), os.path.relpath(os.path.join(r, f), tmp))
                
                with open(z_path, 'rb') as f:
                    await query.message.reply_document(document=f, caption="✅ Build Complete!")
                
                shutil.rmtree(tmp)
                os.remove(z_path)
                await msg.delete()

# --- Execution ---
if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.create_task(start_web_server())
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(init_db).build()
    app.add_handler(CommandHandler("job", start_job))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Conflict Error အတွက် အရေးကြီးဆုံးအပိုင်း
    app.run_polling(drop_pending_updates=True)
