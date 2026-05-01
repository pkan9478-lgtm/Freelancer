import os
import asyncio
import logging
import json
import tempfile
import zipfile
import shutil
import re
import gc
import threading
import aiosqlite
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes, 
    CallbackQueryHandler,
    Application
)
from groq import AsyncGroq
from dotenv import load_dotenv
import redis.asyncio as aioredis

# ==========================================
# 1. Setup & Configurations
# ==========================================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID")
REDIS_URL = os.getenv("REDIS_URL")

if not TELEGRAM_TOKEN or not GROQ_API_KEY or not ADMIN_ID:
    print("❌ Error: API Keys or ADMIN_ID missing.")
    exit()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

os.makedirs('data', exist_ok=True)
DB_NAME = 'data/phogo_ultra_master.db' 
ADMIN_FILTER = filters.User(user_id=int(ADMIN_ID))

FALLBACK_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

# ==========================================
# 2. Render Health Check Server
# ==========================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Live!")
    def log_message(self, format, *args): return

def run_health_check():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# ==========================================
# 3. AI Assistant & Utilities
# ==========================================
async def init_db(app: Application):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('PRAGMA journal_mode=WAL;')
        await db.execute('''CREATE TABLE IF NOT EXISTS job_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, job_desc TEXT, 
                      project_plan TEXT, proposal TEXT, price INTEGER, timeline TEXT, 
                      tech_stack TEXT, generated_code TEXT, status TEXT DEFAULT '1. Gathering')''')
        await db.commit()

class PhoGoUltraAssistant:
    def __init__(self):
        self.groq_client = AsyncGroq(api_key=GROQ_API_KEY)
        self.semaphore = asyncio.Semaphore(1) 

    async def get_ai_response(self, prompt, system_msg, response_format=None):
        messages = [{"role": "system", "content": system_msg}, {"role": "user", "content": prompt}]
        async with self.semaphore:
            for model_name in FALLBACK_MODELS:
                try:
                    kwargs = {"messages": messages, "model": model_name, "temperature": 0.4}
                    if response_format: kwargs["response_format"] = {"type": "json_object"}
                    completion = await self.groq_client.chat.completions.create(**kwargs)
                    res = completion.choices[0].message.content
                    gc.collect()
                    return res
                except Exception as e:
                    logger.warning(f"⚠️ API Error: {e}")
                    await asyncio.sleep(2)
            return None

assistant = PhoGoUltraAssistant()

def create_project_zip(files_dict, job_id):
    temp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(tempfile.gettempdir(), f"Project_Job{job_id}.zip")
    try:
        for path, content in files_dict.items():
            f_path = os.path.join(temp_dir, path)
            os.makedirs(os.path.dirname(f_path), exist_ok=True)
            with open(f_path, 'w', encoding='utf-8') as f: f.write(str(content))
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(temp_dir):
                for f in files: z.write(os.path.join(root, f), os.path.relpath(os.path.join(root, f), temp_dir))
        return zip_path
    finally:
        shutil.rmtree(temp_dir)
        gc.collect()

# ==========================================
# 4. Core Logic (Fixed Parameter Binding)
# ==========================================
async def handle_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("❌ `/job <အကြောင်းအရာ>`")
    desc = " ".join(context.args)
    msg = await update.message.reply_text("🧠 Architecture စတင်ရေးဆွဲနေပါသည်...")
    
    sys_prompt = "Output ONLY JSON with keys: proposal, price (int), timeline, tech_stack (string/list), project_plan (string/list)."
    raw = await assistant.get_ai_response(desc, sys_prompt, response_format=True)
    
    if not raw: return await msg.edit_text("❌ AI Error")
    
    try:
        data = json.loads(raw)
        # Fix: SQLite Binding Error ကိုဖြေရှင်းရန် List များကို String ပြောင်းခြင်း
        p_plan = "\n".join(data['project_plan']) if isinstance(data['project_plan'], list) else str(data['project_plan'])
        t_stack = ", ".join(data['tech_stack']) if isinstance(data['tech_stack'], list) else str(data['tech_stack'])
        
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute(
                "INSERT INTO job_history (user_id, job_desc, project_plan, proposal, price, timeline, tech_stack) VALUES (?,?,?,?,?,?,?)",
                (update.effective_user.id, desc, p_plan, str(data['proposal']), int(data['price']), str(data['timeline']), t_stack)
            )
            job_id = cur.lastrowid
            await db.commit()

        kb = [[InlineKeyboardButton("🚀 Build Source Code (Zip)", callback_query_data=f'step_code_{job_id}')]]
        await msg.edit_text(f"🎯 **Job {job_id} Ready!**\n💰 Budget: ${data['price']}\n🛠 Tech: {t_stack}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Processing Error: {e}")
        await msg.edit_text(f"❌ Error: {str(e)}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    job_id = data.split('_')[-1]

    if data.startswith('step_code_'):
        status_msg = await query.message.reply_text("⚙️ Generating all files...")
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute('SELECT project_plan, tech_stack FROM job_history WHERE id=?', (job_id,)) as cur:
                row = await cur.fetchone()
        
        p = f"Build full project files as JSON (path: code) for: {row[0]}"
        res = await assistant.get_ai_response(p, f"Senior Engineer for {row[1]}. JSON ONLY.", response_format=True)
        
        if res:
            files = json.loads(res)
            zip_p = create_project_zip(files, job_id)
            with open(zip_p, 'rb') as f:
                await query.message.reply_document(document=f, caption=f"✅ Job {job_id} Build Complete.")
            os.remove(zip_p)
            await status_msg.delete()

# ==========================================
# 5. Main
# ==========================================
if __name__ == '__main__':
    threading.Thread(target=run_health_check, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(init_db).build()
    app.add_handler(CommandHandler("job", handle_job, filters=ADMIN_FILTER))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & ADMIN_FILTER, lambda u, c: u.message.reply_text("💬 /job ကိုသုံးပါ။")))
    app.run_polling(drop_pending_updates=True)
