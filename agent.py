import os
import json
import asyncio
import random
import logging
import redis
import gc
import threading
import http.server
import socketserver
import requests
from datetime import datetime
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from poe_api_wrapper import PoeApi
from telegram import Bot

# --- Configuration & Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("PhoGo_AutoIncome_Gen")

PORT = int(os.environ.get("PORT", 10000))

def run_health_server():
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args): pass
    with socketserver.TCPServer(("", PORT), QuietHandler) as httpd:
        httpd.serve_forever()

def self_ping():
    """Render/PaaS များတွင် မအိပ်သွားစေရန် Self-ping လုပ်ပေးခြင်း"""
    url = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")
    import time
    time.sleep(60)
    while True:
        try: requests.get(url, timeout=10)
        except: pass
        time.sleep(300)

class AutoIncomeGenerator:
    def __init__(self):
        # Redis & Telegram Setup
        self.redis = redis.from_url(os.getenv("REDIS_URL"), decode_responses=True)
        self.bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
        self.user_id = os.getenv("TELEGRAM_USER_ID")
        
        # Selectors (Freelancer.com UI updates ဖြစ်ပါက ဤနေရာတွင် ပြင်ရန်)
        self.ui = {
            "login_email": "input[type='email']",
            "login_pass": "input[type='password']",
            "job_card": ".JobSearchCard-item",
            "job_title": ".JobSearchCard-primary-heading a",
            "job_desc": ".JobSearchCard-primary-description",
            "proposal_box": "textarea#description", 
            "amount_field": "input#bid",
            "days_field": "input#period",
            "chat_threads": "fl-message-thread-item",
            "chat_messages": "fl-message-bubble-text",
            "message_box": "textarea[placeholder*='Type a message']",
            "file_upload": "input[type='file']",
            "send_msg_btn": "button[data-color='secondary']",
            "milestone_badge": ".Milestone-badge--funded" 
        }

    async def notify(self, msg):
        try: await self.bot.send_message(self.user_id, msg, parse_mode='HTML')
        except Exception as e: logger.error(f"Telegram Notify Error: {e}")

    async def get_ai_brain(self, prompt, model="Claude-3.5-Sonnet"):
        """Poe API ကို PB နှင့် M-LAT cookie နှစ်ခုလုံးသုံးပြီး ချိတ်ဆက်ခြင်း"""
        try:
            tokens = {
                "p-b": os.getenv("POE_PB_COOKIE"),
                "p-lat": os.getenv("POE_M_LAT_COOKIE")
            }
            client = PoeApi(tokens)
            res = ""
            for chunk in client.send_message(model, prompt):
                if "response" in chunk: res = chunk["response"]
                elif "text" in chunk: res = chunk["text"]
            return res
        except Exception as e:
            logger.error(f"AI Engine (Poe) Error: {e}")
            return None

    def extract_code_to_buffer(self, ai_output, file_prefix):
        """AI Output ထဲမှ Code များကို သီးသန့်ထုတ်ယူပြီး Memory Buffer အဖြစ်ပြောင်းခြင်း"""
        data = ai_output
        if "```" in ai_output:
            parts = ai_output.split("```")
            if len(parts) > 1:
                data = parts[1].split("\n", 1)[-1] 
        
        file_name = f"{file_prefix}_{int(datetime.now().timestamp())}.py"
        file_buffer = data.strip().encode('utf-8')
        return {"name": file_name, "mimeType": "text/x-python", "buffer": file_buffer}

    async def human_type(self, element, text):
        """Bot ဟု မရိပ်မိစေရန် လူရိုက်သကဲ့သို့ နှေးနှေးရိုက်ခြင်း"""
        await element.fill("")
        await element.type(text, delay=random.randint(30, 85))

    async def handle_login(self, page):
        """Cookie သို့မဟုတ် Login Form ဖြင့် ဝင်ရောက်ခြင်း"""
        cookie_data = self.redis.get("freelancer_session_cookies")
        if cookie_data:
            try:
                await page.context.add_cookies(json.loads(cookie_data))
                await page.goto("https://www.freelancer.com/dashboard")
                if "dashboard" in page.url:
                    logger.info("Login successful via Cookies.")
                    return True
            except: pass

        logger.info("Initiating Manual-style Login...")
        await page.goto("https://www.freelancer.com/login")
        await asyncio.sleep(4)
        await self.human_type(page.locator(self.ui["login_email"]), os.getenv("FL_EMAIL"))
        await self.human_type(page.locator(self.ui["login_pass"]), os.getenv("FL_PASSWORD"))
        await page.click("button[type='submit']")
        await asyncio.sleep(15) # 2FA ရှိပါက Telegram မှတဆင့် manual ဖြေရန် အချိန်ပေးခြင်း
        
        cookies = await page.context.cookies()
        self.redis.set("freelancer_session_cookies", json.dumps(cookies))
        return True

    async def handle_negotiations_and_delivery(self, page):
        """စကားပြောဆိုခြင်းနှင့် အလုပ်အပ်နှံခြင်း အပိုင်း"""
        logger.info("Checking messages...")
        await page.goto("https://www.freelancer.com/messages", wait_until="domcontentloaded")
        await asyncio.sleep(8)
        
        threads = await page.query_selector_all(self.ui["chat_threads"])
        for thread in threads[:3]: 
            await thread.click()
            await asyncio.sleep(5)
            
            messages = await page.query_selector_all(self.ui["chat_messages"])
            if not messages: continue
            
            chat_history = [await m.inner_text() for m in messages[-5:]]
            last_msg = chat_history[-1]
            chat_id = str(hash(last_msg)) # ရိုးရှင်းသော ID သတ်မှတ်ချက်
            
            if self.redis.get(f"replied:{chat_id}"): continue

            # Milestone စစ်ဆေးခြင်း
            is_funded = await page.query_selector(self.ui["milestone_badge"]) is not None

            if not is_funded:
                # အလုပ်ရရန် ဆွေးနွေးခြင်း
                prompt = (f"Client says: '{last_msg}'. Chat history: {chat_history}. "
                          "Reply as a pro developer to get this project awarded. Short & professional.")
                reply_text = await self.get_ai_brain(prompt)
                if reply_text:
                    await self.human_type(page.locator(self.ui["message_box"]), reply_text)
                    await page.click(self.ui["send_msg_btn"])
                    self.redis.setex(f"replied:{chat_id}", 86400, "done")
                    await self.notify(f"💬 <b>Replied:</b> {reply_text}")

            else:
                # ငွေသွင်းပြီးပါက Code ထုတ်ပေးပြီး ပို့ခြင်း
                if not self.redis.get(f"delivered:{chat_id}"):
                    logger.info("Milestone detected! Generating final code...")
                    prompt = (f"Based on: {chat_history}, generate the full Python/React code solution. "
                              "Return only code in markdown.")
                    ai_code = await self.get_ai_brain(prompt)
                    if ai_code:
                        file_data = self.extract_code_to_buffer(ai_code, "Solution")
                        # File upload (Playwright handles buffer)
                        await page.locator(self.ui["file_upload"]).set_input_files(files=[file_data])
                        await asyncio.sleep(5)
                        
                        delivery_note = "Project completed. Code attached. Please review and release the milestone. Thanks!"
                        await self.human_type(page.locator(self.ui["message_box"]), delivery_note)
                        await page.click(self.ui["send_msg_btn"])
                        
                        self.redis.setex(f"delivered:{chat_id}", 2592000, "done")
                        await self.notify("✅ <b>Project Delivered!</b>")

    async def execute_bidding(self, page):
        """Bidding ဆွဲသည့် အပိုင်း"""
        search_url = "https://www.freelancer.com/search/projects?q=python%20automation%20bot%20scraper"
        await page.goto(search_url, wait_until="domcontentloaded")
        await asyncio.sleep(6)
        
        jobs = await page.query_selector_all(self.ui["job_card"])
        for job in jobs[:2]:
            title_elem = await job.query_selector(self.ui["job_title"])
            if not title_elem: continue
            
            title = await title_elem.inner_text()
            link = await title_elem.get_attribute("href")
            jid = link.split("/")[-1]
            
            if not self.redis.get(f"bid_done:{jid}"):
                desc = await (await job.query_selector(self.ui["job_desc"])).inner_text()
                prompt = f"Write a technical bid for: {title}. Desc: {desc}. Max 300 chars. No 'Hi'."
                proposal = await self.get_ai_brain(prompt)
                
                if proposal:
                    await page.goto(f"https://www.freelancer.com{link}")
                    await asyncio.sleep(6)
                    if await page.query_selector(self.ui["proposal_box"]):
                        await self.human_type(page.locator(self.ui["proposal_box"]), proposal)
                        await page.fill(self.ui["amount_field"], "30")
                        await page.fill(self.ui["days_field"], "2")
                        # await page.click("button.PlaceBid-btn") # လက်တွေ့သုံးရန် comment ဖြုတ်ပါ
                        self.redis.setex(f"bid_done:{jid}", 604800, "done")
                        await self.notify(f"🚀 <b>Bid Sent:</b> {title}")

    async def system_core(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0")
            page = await context.new_page()
            await stealth_async(page)

            try:
                await self.handle_login(page)
                while True:
                    await self.execute_bidding(page)
                    await self.handle_negotiations_and_delivery(page)
                    
                    gc.collect() # Memory Leak မဖြစ်အောင် ရှင်းထုတ်ခြင်း
                    wait = random.randint(1200, 2400)
                    logger.info(f"Cycle finished. Sleeping {wait}s...")
                    await asyncio.sleep(wait)
            except Exception as e:
                logger.error(f"Core Error: {e}")
                await self.notify(f"🚨 <b>Error:</b> {str(e)[:100]}")
            finally:
                await browser.close()

if __name__ == "__main__":
    # Background Services
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    
    # Start Generator
    engine = AutoIncomeGenerator()
    asyncio.run(engine.system_core())
