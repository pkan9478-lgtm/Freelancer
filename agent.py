import os
import asyncio
import logging
import json
import tempfile
import zipfile
import shutil
import re
import gc
import aiosqlite
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
import redis.asyncio as aioredis # Added Redis

# ==========================================
# 1. Setup & Configurations
# ==========================================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

if not TELEGRAM_TOKEN or not GROQ_API_KEY or not ADMIN_ID:
    print("❌ Error: API Keys or ADMIN_ID missing in .env file.")
    exit()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Save DB in a persistent directory
os.makedirs('data', exist_ok=True)
DB_NAME = 'data/phogo_ultra_master.db' 
ADMIN_FILTER = filters.User(user_id=int(ADMIN_ID))

FALLBACK_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768"
]

# Redis Client Setup (Replaces in-memory admin_states)
redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

# ==========================================
# 2. Optimized Database Setup
# ==========================================
async def init_db(app: Application):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('PRAGMA journal_mode=WAL;')
        await db.execute('''CREATE TABLE IF NOT EXISTS job_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER, 
                      job_desc TEXT, 
                      project_plan TEXT,
                      proposal TEXT, 
                      price INTEGER, 
                      timeline TEXT, 
                      tech_stack TEXT, 
                      generated_code TEXT, 
                      status TEXT DEFAULT '1. Requirements Gathering',
                      timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        await db.commit()
    logger.info("✅ Database & Redis Initialized (Memory Optimized)!")

# ==========================================
# 3. AI Assistant Class (Robust Rate Limit)
# ==========================================
class PhoGoUltraAssistant:
    def __init__(self):
        self.groq_client = AsyncGroq(api_key=GROQ_API_KEY)
        self.semaphore = asyncio.Semaphore(1) 

    async def get_ai_response(self, prompt, system_msg, response_format=None):
        messages = [{"role": "system", "content": system_msg}, {"role": "user", "content": prompt}]
        
        async with self.semaphore:
            for model_name in FALLBACK_MODELS:
                backoff_time = 2
                for attempt in range(3): 
                    try:
                        kwargs = {"messages": messages, "model": model_name, "temperature": 0.4}
                        if response_format:
                            kwargs["response_format"] = {"type": "json_object"}

                        completion = await self.groq_client.chat.completions.create(**kwargs)
                        result = completion.choices[0].message.content
                        
                        # Free up memory explicitly
                        del completion
                        gc.collect() 
                        return result
                    
                    except Exception as e:
                        logger.warning(f"⚠️ Groq API Error ({model_name} - Attempt {attempt + 1}): {e}")
                        if "rate_limit" in str(e).lower() or "429" in str(e):
                            await asyncio.sleep(backoff_time)
                            backoff_time *= 2 
                        else:
                            await asyncio.sleep(2)
                            break 
            return None

    async def analyze_and_plan_job(self, job_desc):
        sys_msg = (
            "You are a Senior Solutions Architect. Analyze the job and output ONLY valid JSON. "
            "Keys needed: 'proposal', 'price' (int, USD), 'timeline', 'tech_stack', 'project_plan'."
        )
        response = await self.get_ai_response(f"Job: {job_desc}", sys_msg, response_format=True)
        if not response: return None
        try:
            parsed_json = json.loads(re.sub(r'```json\s*|\s*```', '', response, flags=re.IGNORECASE).strip())
            del response # Memory optimization
            gc.collect()
            return parsed_json
        except Exception as e:
            logger.error(f"JSON Parse Error: {e}")
            return None

assistant = PhoGoUltraAssistant()

# ==========================================
# 4. Helpers (Memory-Efficient Zip Generator)
# ==========================================
async def keep_typing(chat_id, context, action_type=ChatAction.TYPING):
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=action_type)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass

def create_project_zip(files_dict, job_id):
    temp_dir = tempfile.mkdtemp()
    zip_filename = f"Project_Build_Job{job_id}.zip"
    zip_filepath = os.path.join(tempfile.gettempdir(), zip_filename)

    try:
        for file_path, file_content in files_dict.items():
            full_path = os.path.join(temp_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(str(file_content))

        with zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    file_p = os.path.join(root, file)
                    arcname = os.path.relpath(file_p, temp_dir)
                    zipf.write(file_p, arcname)
                    
        return zip_filepath
    finally:
        shutil.rmtree(temp_dir)
        # Force GC to clear file operations from memory
        gc.collect()

# ==========================================
# 5. Core Handlers
# ==========================================
async def process_new_job(job_desc, user_id, message_obj, context):
    processing_msg = await message_obj.reply_text("🧠 Project Plan စတင်ရေးဆွဲနေပါသည်...")
    typing_task = asyncio.create_task(keep_typing(message_obj.chat_id, context))

    try:
        analysis = await assistant.analyze_and_plan_job(job_desc)
        if not analysis:
            return await processing_msg.edit_text("❌ API Error ဖြစ်ပေါ်နေပါသည်။ နောက်မှ ထပ်မံကြိုးစားပါ။")

        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute('''INSERT INTO job_history 
                                         (user_id, job_desc, project_plan, proposal, price, timeline, tech_stack)
                                         VALUES (?, ?, ?, ?, ?, ?, ?)''', 
                                      (user_id, job_desc, analysis.get('project_plan'), analysis.get('proposal'), 
                                       analysis.get('price', 0), analysis.get('timeline'), analysis.get('tech_stack')))
            job_id = cursor.lastrowid
            await db.commit()

        keyboard = [[InlineKeyboardButton("🚀 1. Build Entire System (Zip)", callback_query_data=f'step_code_{job_id}')]]
        
        result_text = (
            f"🎯 **System Architecture Ready! (ID: `{job_id}`)**\n\n"
            f"🗣️ **Req:** _{job_desc[:100]}..._\n"
            f"💰 **Budget:** ${analysis.get('price', 0)} | ⏳ **Time:** {analysis.get('timeline')}\n"
            f"🛠 **Tech:** {analysis.get('tech_stack')}\n\n"
            "အောက်ပါခလုတ်ကို နှိပ်၍ Code အားလုံးကို Zip ဖြင့် ထုတ်ယူပါ။"
        )
        await processing_msg.edit_text(result_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        
        del analysis
        gc.collect()
    finally:
        typing_task.cancel()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_input = update.message.text

    # Check state from Redis instead of Memory Dict
    state_data_str = await redis_client.get(f"state:{user_id}")
    
    if state_data_str:
        state_data = json.loads(state_data_str)
        job_id = state_data['job_id']
        action = state_data['action']
        
        await redis_client.delete(f"state:{user_id}") # Clear state
        await handle_code_iteration(update, context, job_id, action, user_input)
        return

    await update.message.reply_text("💬 Project အသစ်စတင်ရန် `/job <အကြောင်းအရာ>` ဟု ရိုက်ထည့်ပါ။")

async def handle_code_iteration(update: Update, context: ContextTypes.DEFAULT_TYPE, job_id, action, instruction):
    processing_msg = await update.message.reply_text("⚙️ System ကို Update ပြုလုပ်နေပါသည်...")
    typing_task = asyncio.create_task(keep_typing(update.effective_chat.id, context))

    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute('SELECT job_desc, tech_stack, generated_code FROM job_history WHERE id=?', (job_id,)) as cursor:
                row = await cursor.fetchone()
        
        if not row or not row[2]: return await processing_msg.edit_text("❌ DB Error: ယခင် Code များကို ရှာမတွေ့ပါ။")
        job_desc, tech_stack, current_code_json = row

        sys_prompt = (
            f"You are a Principal {tech_stack} Engineer. Update the system based on instruction. "
            "Output ONLY a valid JSON object. Keys: file paths, Values: RAW code."
        )
        prompt = f"Original: {job_desc}\n\nCurrent:\n{current_code_json}\n\nNEW INSTRUCTION ({action}): {instruction}"

        # Free memory before API call
        del current_code_json
        gc.collect()

        response = await assistant.get_ai_response(prompt, sys_prompt, response_format=True)
        
        if response:
            try:
                files_dict = json.loads(response)
                
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("UPDATE job_history SET generated_code=?, status='3. Review' WHERE id=?", (json.dumps(files_dict), job_id))
                    await db.commit()

                zip_path = create_project_zip(files_dict, job_id)
                
                iter_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add New Feature", callback_query_data=f'iter_add_{job_id}'),
                     InlineKeyboardButton("🐛 Fix Bug/Error", callback_query_data=f'iter_fix_{job_id}')],
                    [InlineKeyboardButton("🧪 Mark as Testing", callback_query_data=f'step_test_{job_id}')]
                ])
                
                await processing_msg.delete()
                with open(zip_path, 'rb') as f:
                    await update.message.reply_document(
                        document=f, 
                        caption=f"✅ **System Updated (Job {job_id})**\nညွှန်ကြားချက်: _{instruction}_", 
                        reply_markup=iter_kb,
                        parse_mode='Markdown'
                    )
                os.remove(zip_path)
                
                del files_dict, response
                gc.collect()
            except Exception as e:
                logger.error(f"Iteration Error: {e}")
                await processing_msg.edit_text("❌ Error ဖြစ်သွားပါသည်။")
        else:
            await processing_msg.edit_text("❌ API အခက်အခဲရှိပါသည်။")
    finally:
        typing_task.cancel()

# ==========================================
# 6. Callback Handlers (Redis Integrated)
# ==========================================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if str(query.from_user.id) != ADMIN_ID: return await query.answer("⛔ Access Denied.", show_alert=True)
    await query.answer()
    data = query.data

    if data.startswith('step_code_'):
        job_id = data.split('_')[-1]
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute('SELECT project_plan, tech_stack FROM job_history WHERE id=?', (job_id,)) as cursor:
                row = await cursor.fetchone()
        
        plan, tech_stack = row
        status_msg = await query.message.reply_text("⚙️ System တစ်ခုလုံးကို တည်ဆောက်နေပါသည်...")
        typing_task = asyncio.create_task(keep_typing(update.effective_chat.id, context))

        try:
            sys_prompt = f"You are a Principal {tech_stack} Architect. Create codebase JSON from plan."
            response = await assistant.get_ai_response(f"Plan:\n{plan}", sys_prompt, response_format=True)
            
            if response:
                try:
                    files_dict = json.loads(response)
                    
                    async with aiosqlite.connect(DB_NAME) as db:
                        await db.execute("UPDATE job_history SET generated_code=?, status='2. Coding Done' WHERE id=?", (json.dumps(files_dict), job_id))
                        await db.commit()

                    zip_path = create_project_zip(files_dict, job_id)
                    
                    iter_kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("➕ Add Feature", callback_query_data=f'iter_add_{job_id}'),
                         InlineKeyboardButton("🐛 Fix Bug", callback_query_data=f'iter_fix_{job_id}')],
                        [InlineKeyboardButton("🧪 Mark as Testing", callback_query_data=f'step_test_{job_id}')]
                    ])
                    await status_msg.delete()
                    with open(zip_path, 'rb') as f:
                        await query.message.reply_document(document=f, caption="✅ **Full System Build Complete!**", reply_markup=iter_kb, parse_mode='Markdown')
                    os.remove(zip_path) 
                    
                    # Memory Cleanup
                    del files_dict, response
                    gc.collect()
                except Exception as e:
                    logger.error(f"Zip Creation Error: {e}")
                    await status_msg.edit_text("❌ Error ဖြစ်သွားပါသည်။")
            else:
                await status_msg.edit_text("❌ API အခက်အခဲရှိပါသည်။")
        finally:
            typing_task.cancel()

    elif data.startswith('iter_'):
        parts = data.split('_')
        action, job_id = parts[1], parts[2]
        
        # Save state to Redis instead of memory
        state_data = {'action': 'Add Feature' if action == 'add' else 'Fix Bug', 'job_id': job_id}
        await redis_client.set(f"state:{ADMIN_ID}", json.dumps(state_data), ex=3600) # Expire in 1 hour
        
        await query.message.reply_text("👇 ထပ်မံထည့်သွင်းလိုသော အချက်ကို ရိုက်ထည့်ပါ။", reply_markup=ForceReply(selective=True))

    elif data.startswith('step_test_'):
        job_id = data.split('_')[-1]
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE job_history SET status='4. Testing & Review' WHERE id=?", (job_id,))
            await db.commit()
            
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🤝 Client Approved", callback_query_data=f'step_approve_{job_id}')]])
        await query.edit_message_caption(caption=f"🧪 **Job {job_id} Testing Mode**", reply_markup=kb, parse_mode='Markdown')

    elif data.startswith('step_approve_'):
        job_id = data.split('_')[-1]
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE job_history SET status='5. Completed' WHERE id=?", (job_id,))
            await db.commit()
            
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💰 Mark as Paid", callback_query_data=f'step_paid_{job_id}')]])
        await query.edit_message_caption(caption=f"✅ **Job {job_id} Completed!**", reply_markup=kb, parse_mode='Markdown')

    elif data.startswith('step_paid_'):
        job_id = data.split('_')[-1]
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE job_history SET status='6. Paid ✅' WHERE id=?", (job_id,))
            await db.commit()
            
        await query.edit_message_caption(caption=f"🎉 **Job {job_id} Fully Closed!**", reply_markup=None, parse_mode='Markdown')

async def handle_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("❌ `/job <အလုပ်အကြောင်းအရာ>` ရိုက်ထည့်ပါ။")
    await process_new_job(" ".join(context.args), update.effective_user.id, update.message, context)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(init_db).build()
    app.add_handler(CommandHandler("job", handle_job, filters=ADMIN_FILTER))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & ADMIN_FILTER, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    print("🔒 Ultra End-to-End System Active (Memory Optimized)...")
    app.run_polling(drop_pending_updates=True)
