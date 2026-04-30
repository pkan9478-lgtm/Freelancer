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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("PhoGo_AutoIncome_Gen")

PORT = int(os.environ.get("PORT", 10000))

def run_health_server():
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args): pass
    with socketserver.TCPServer(("", PORT), QuietHandler) as httpd:
        httpd.serve_forever()

def self_ping():
    url = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")
    import time
    time.sleep(60)
    while True:
        try: requests.get(url, timeout=10)
        except: pass
        time.sleep(300)

class AutoIncomeGenerator:
    def __init__(self):
        self.redis = redis.from_url(os.getenv("REDIS_URL"), decode_responses=True)
        self.bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
        self.user_id = os.getenv("TELEGRAM_USER_ID")
        
        self.ui = {
            "login_email": "input[type='email']",
            "login_pass": "input[type='password']",
            "job_card": ".JobSearchCard-item",
            "job_title": ".JobSearchCard-primary-heading a",
            "job_desc": ".JobSearchCard-primary-description",
            "proposal_box": "textarea#description", 
            "amount_field": "input#bid",
            "days_field": "input#period",
            
            # Chat & Negotiation Selectors
            "chat_threads": "fl-message-thread-item",
            "chat_messages": "fl-message-bubble-text",
            "message_box": "textarea[placeholder*='Type a message']",
            "file_upload": "input[type='file']",
            "send_msg_btn": "button[data-color='secondary']",
            "milestone_badge": ".Milestone-badge--funded" # ငွေကြိုသွင်းထားကြောင်း ပြသည့်အမှတ်အသား
        }

    async def notify(self, msg):
        try: await self.bot.send_message(self.user_id, msg, parse_mode='HTML')
        except: pass

    async def get_ai_brain(self, prompt, model="Claude-3.5-Sonnet"):
        try:
            client = PoeApi(os.getenv("POE_PB_COOKIE"))
            res = ""
            for chunk in client.send_message(model, prompt):
                if "response" in chunk: res = chunk["response"]
                elif "text" in chunk: res = chunk["text"]
            return res
        except Exception as e:
            logger.error(f"AI Engine Error: {e}")
            return None

    def extract_code_to_buffer(self, ai_output, file_prefix):
        data = ai_output
        if "```" in ai_output:
            parts = ai_output.split("```")
            if len(parts) > 1:
                data = parts[1].split("\n", 1)[-1] 
        file_name = f"{file_prefix}_{int(datetime.now().timestamp())}.py"
        file_buffer = data.strip().encode('utf-8')
        return {"name": file_name, "mimeType": "text/x-python", "buffer": file_buffer}

    async def human_type(self, element, text):
        await element.fill("")
        await element.type(text, delay=random.randint(30, 80))

    async def handle_login(self, page):
        cookie_data = self.redis.get("freelancer_session_cookies")
        if cookie_data:
            try:
                await page.context.add_cookies(json.loads(cookie_data))
                await page.goto("https://www.freelancer.com/dashboard")
                if "dashboard" in page.url:
                    return True
            except: pass

        logger.info("Auto-Login initiated...")
        await page.goto("https://www.freelancer.com/login")
        await asyncio.sleep(3)
        await self.human_type(page.locator(self.ui["login_email"]), os.getenv("FL_EMAIL"))
        await self.human_type(page.locator(self.ui["login_pass"]), os.getenv("FL_PASSWORD"))
        await page.click("button[type='submit']")
        await asyncio.sleep(15) 
        
        cookies = await page.context.cookies()
        self.redis.set("freelancer_session_cookies", json.dumps(cookies))
        return True

    async def handle_negotiations_and_delivery(self, page):
        """[ADVANCED] စကားပြောခြင်း၊ Milestone စစ်ဆေးခြင်းနှင့် အလိုအလျောက် File ပို့ခြင်း"""
        logger.info("Scanning Inbox for Negotiations and Active Projects...")
        await page.goto("https://www.freelancer.com/messages", wait_until="domcontentloaded")
        await asyncio.sleep(8)
        
        threads = await page.query_selector_all(self.ui["chat_threads"])
        
        for thread in threads[:5]: # နောက်ဆုံး Chat ၅ ခုကိုသာ စစ်မည်
            await thread.click()
            await asyncio.sleep(4)
            
            # Chat သမိုင်းကြောင်းကို ဖတ်ယူခြင်း
            messages = await page.query_selector_all(self.ui["chat_messages"])
            if not messages: continue
            
            chat_history = []
            for msg in messages[-5:]: # နောက်ဆုံး စာ ၅ ကြောင်း
                chat_history.append(await msg.inner_text())
                
            last_msg = chat_history[-1]
            chat_id = str(hash(last_msg))
            
            if self.redis.get(f"replied:{chat_id}"): continue

            # ၁။ Milestone Funded ဖြစ်မဖြစ် စစ်ဆေးခြင်း
            is_funded = await page.query_selector(self.ui["milestone_badge"]) is not None

            # ---------------------------------------------------------
            # PHASE A: အလုပ်မရသေးခင် ညှိနှိုင်းခြင်း (Pre-award Chat)
            # ---------------------------------------------------------
            if not is_funded:
                logger.info("Negotiating with Client...")
                prompt = (f"Act as Pho Go, a professional Software Architect. "
                          f"The client messaged you: '{last_msg}'. "
                          f"Previous context: {chat_history[:-1]}. "
                          "Reply concisely and confidently to convince them to award the project and fund the milestone. "
                          "Max 2 sentences. No robotic greetings.")
                
                reply_text = await self.get_ai_brain(prompt)
                if reply_text:
                    await self.human_type(page.locator(self.ui["message_box"]), reply_text)
                    await page.click(self.ui["send_msg_btn"])
                    self.redis.setex(f"replied:{chat_id}", 86400, "done")
                    await self.notify(f"💬 <b>Auto-Replied to Client:</b> {reply_text}")

            # ---------------------------------------------------------
            # PHASE B: ငွေသွင်းပြီးနောက် Auto-Delivery ပို့ခြင်း
            # ---------------------------------------------------------
            else:
                if not self.redis.get(f"delivered_code:{chat_id}"):
                    logger.info("Milestone Funded! Generating Code & Delivering...")
                    await self.notify("💰 <b>Milestone Funded!</b> Commencing Auto-Delivery...")
                    
                    # ၄ နာရီ - ၈ နာရီ ဟန်ဆောင်အချိန်ဆွဲခြင်း
                    await asyncio.sleep(random.randint(300, 600)) 
                    
                    prompt = (f"Act as Pho Go, Senior Developer. Based on this chat history: {chat_history}, "
                              "generate the FINAL production-ready Python or React code requested by the client. "
                              "Output ONLY the code inside markdown. No explanations.")
                    
                    ai_code = await self.get_ai_brain(prompt)
                    if ai_code:
                        memory_file = self.extract_code_to_buffer(ai_code, "Final_Delivery")
                        await page.locator(self.ui["file_upload"]).set_input_files(files=[memory_file])
                        await asyncio.sleep(5)
                        
                        delivery_msg = ("Hello, I have completed the project. Please find the attached source code. "
                                        "If everything is working perfectly, please **release the milestone**. "
                                        "Let me know if you need any revisions.")
                        
                        await self.human_type(page.locator(self.ui["message_box"]), delivery_msg)
                        await page.click(self.ui["send_msg_btn"])
                        
                        self.redis.setex(f"delivered_code:{chat_id}", 2592000, "done")
                        del memory_file
                        await self.notify("✅ <b>Mission Accomplished!</b> Code Delivered & Milestone Release Requested.")

    async def execute_bidding(self, page):
        """Auto Bidding Engine"""
        await page.goto("https://www.freelancer.com/search/projects?q=python%20react%20automation%20bot", wait_until="domcontentloaded")
        await asyncio.sleep(5)
        
        jobs = await page.query_selector_all(self.ui["job_card"])
        for job in jobs[:2]:
            try:
                title_elem = await job.query_selector(self.ui["job_title"])
                if not title_elem: continue
                
                title = await title_elem.inner_text()
                job_link = await title_elem.get_attribute("href")
                jid = job_link.split("/")[-1] if job_link else None
                
                if jid and not self.redis.get(f"fl_bid:{jid}"):
                    desc_elem = await job.query_selector(self.ui["job_desc"])
                    description = await desc_elem.inner_text() if desc_elem else ""
                    
                    prompt = (f"Act as Pho Go, a Software Architect. Project: {title}\nDesc: {description}\n"
                              "Write a strict, technical proposal (max 400 chars). State you can start immediately. No greetings.")
                    
                    proposal = await self.get_ai_brain(prompt)
                    
                    if proposal:
                        await page.goto(f"https://www.freelancer.com{job_link}", wait_until="domcontentloaded")
                        await asyncio.sleep(7)
                        
                        if await page.query_selector(self.ui["proposal_box"]):
                            await self.human_type(page.locator(self.ui["proposal_box"]), proposal)
                            
                            bid_amount = str(random.randint(20, 60)) # Target $20/day
                            await page.fill(self.ui["amount_field"], bid_amount)
                            await page.fill(self.ui["days_field"], str(random.randint(2, 4)))
                            
                            # await page.click("button.PlaceBid-btn") # တကယ် Bidding စတင်ရန် ဤနေရာကို ဖွင့်ပါ
                            
                            self.redis.setex(f"fl_bid:{jid}", 604800, "done")
                            await self.notify(f"🚀 <b>Bid Placed:</b> {title} | Amount: ${bid_amount}")
            except Exception as e:
                pass

    async def system_core(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, 
                args=[
                    "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process",
                    "--js-flags=--max-old-space-size=256", "--disable-blink-features=AutomationControlled"
                ]
            )
            context = await browser.new_context(
                viewport={'width': 1366, 'height': 768},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await stealth_async(page)

            try:
                await self.handle_login(page)
                while True:
                    await self.execute_bidding(page)
                    await self.handle_negotiations_and_delivery(page) # Advanced Negotiation & Delivery
                    gc.collect() 
                    
                    sleep_time = random.randint(1800, 3600)
                    logger.info(f"Cycle completed. Memory cleared. Sleeping {sleep_time}s")
                    await asyncio.sleep(sleep_time)
            except Exception as e:
                logger.critical(f"System Crash: {e}")
                await self.notify(f"🚨 <b>Critical Error:</b> {str(e)[:150]}")
            finally:
                await browser.close()

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    engine = AutoIncomeGenerator()
    asyncio.run(engine.system_core())
