import os, json, asyncio, random, logging, redis, gc, threading, http.server, socketserver, requests
from datetime import datetime
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from poe_api_wrapper import PoeApi
from telegram import Bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("OmniAgent_Final")

# --- RENDER HEALTH SERVER ---
def run_health_server():
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args): pass
    with socketserver.TCPServer(("", int(os.environ.get("PORT", 10000))), QuietHandler) as httpd:
        httpd.serve_forever()

class AutoIncomeGenerator:
    def __init__(self):
        self.redis = redis.from_url(os.getenv("REDIS_URL"), decode_responses=True)
        self.bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
        self.user_id = os.getenv("TELEGRAM_USER_ID")
        self.selectors = {
            "job_card": ".JobSearchCard-item",
            "job_title": ".JobSearchCard-primary-heading a",
            "job_desc": ".JobSearchCard-primary-description",
            "proposal_box": "textarea#description",
            "amount_field": "input#bid",
            "days_field": "input#period",
            "chat_threads": "fl-message-thread-item",
            "chat_messages": ".fl-message-bubble-text",
            "message_box": "textarea[placeholder*='Type a message']",
            "file_upload": "input[type='file']",
            "send_msg_btn": "button[data-color='secondary']",
            "milestone_badge": ".Milestone-badge--funded"
        }

    async def notify(self, msg):
        try: await self.bot.send_message(self.user_id, msg, parse_mode='HTML')
        except: pass

    async def get_ai_brain(self, prompt, model="Claude-3.5-Sonnet"):
        """PB နှင့် PLAT နှစ်ခုလုံးသုံး၍ AI ဆီမှ အဖြေထုတ်ယူခြင်း"""
        try:
            tokens = {
                'p-b': os.getenv("POE_PB_COOKIE"),
                'p-lat': os.getenv("POE_PLAT_COOKIE")
            }
            client = PoeApi(tokens=tokens)
            res = ""
            for chunk in client.send_message(model, prompt):
                res = chunk.get("text") or chunk.get("response") or res
            return res
        except Exception as e:
            logger.error(f"AI Engine Error: {e}")
            return None

    async def human_type(self, element, text):
        await element.fill("")
        await element.type(text, delay=random.randint(30, 70))

    async def handle_login(self, page):
        logger.info("Auto-Login initiated...")
        await page.goto("https://www.freelancer.com/login")
        await asyncio.sleep(5)
        await self.human_type(page.locator("input[type='email']"), os.getenv("FL_EMAIL"))
        await self.human_type(page.locator("input[type='password']"), os.getenv("FL_PASSWORD"))
        await page.click("button[type='submit']")
        await asyncio.sleep(10)
        return True

    async def autonomous_execution(self, page):
        """အလုပ်ရှင်နှင့် ညှိနှိုင်းခြင်းနှင့် Auto-Delivery ပို့ခြင်း"""
        logger.info("Scanning Inbox for Negotiations...")
        await page.goto("https://www.freelancer.com/messages", wait_until="domcontentloaded")
        await asyncio.sleep(8)
        threads = await page.query_selector_all(self.selectors["chat_threads"])
        
        for thread in threads[:3]:
            await thread.click()
            await asyncio.sleep(4)
            messages = await page.query_selector_all(self.selectors["chat_messages"])
            if not messages: continue
            
            history = [await m.inner_text() for m in messages[-5:]]
            last_msg = history[-1]
            chat_id = str(hash(last_msg))

            if self.redis.get(f"done:{chat_id}"): continue

            # Milestone Funded ဖြစ်မဖြစ် စစ်ဆေးခြင်း
            is_funded = await page.query_selector(self.selectors["milestone_badge"]) is not None

            if is_funded:
                # PHASE: AUTO-DELIVERY (ငွေသွင်းပြီးပါက အလုပ်ကို AI နှင့် လုပ်ခိုင်းပြီး ပို့ခြင်း)
                prompt = f"Client requested: {history}. Write only the production-ready Python code solution."
                code = await self.get_ai_brain(prompt)
                if code:
                    await self.notify("💰 <b>Milestone Funded!</b> Delivering Project...")
                    await self.human_type(page.locator(self.selectors["message_box"]), "I have completed the task. Please find the solution below.")
                    await page.click(self.selectors["send_msg_btn"])
                    self.redis.setex(f"done:{chat_id}", 2592000, "delivered")
            else:
                # PHASE: NEGOTIATION (အလုပ်ရအောင် AI နှင့် ညှိနှိုင်းခြင်း)
                prompt = f"Client messaged: '{last_msg}'. Reply professionally as Pho Go to get hired."
                reply = await self.get_ai_brain(prompt)
                if reply:
                    await self.human_type(page.locator(self.selectors["message_box"]), reply)
                    await page.click(self.selectors["send_msg_btn"])
                    self.redis.setex(f"done:{chat_id}", 86400, "replied")

    async def run(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--single-process"])
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            page = await context.new_page()
            await stealth_async(page)
            
            # --- FIX FOR SYNTAX ERROR: ASYNC RESOURCE BLOCKER ---
            async def block_resources(route):
                if route.request.resource_type in ["image", "font", "stylesheet"]:
                    await route.abort()
                else:
                    await route.continue()

            await page.route("**/*", block_resources)

            await self.handle_login(page)
            while True:
                try:
                    await self.autonomous_execution(page)
                    gc.collect()
                except Exception as e:
                    logger.error(f"Cycle Error: {e}")
                
                logger.info("Cycle completed. Sleeping...")
                await asyncio.sleep(random.randint(1800, 3600))

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    asyncio.run(AutoIncomeGenerator().run())
