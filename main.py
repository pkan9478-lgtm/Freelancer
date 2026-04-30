import os, json, asyncio, random, logging, redis, gc, threading, http.server, socketserver, requests
from datetime import datetime
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from poe_api_wrapper import PoeApi
from telegram import Bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("OmniAgent_V2")

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
        """PB နှင့် PLAT Cookie နှစ်ခုလုံးသုံး၍ AI Response ယူခြင်း"""
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
        logger.info("Checking Session...")
        await page.goto("https://www.freelancer.com/login")
        await asyncio.sleep(3)
        await self.human_type(page.locator("input[type='email']"), os.getenv("FL_EMAIL"))
        await self.human_type(page.locator("input[type='password']"), os.getenv("FL_PASSWORD"))
        await page.click("button[type='submit']")
        await asyncio.sleep(10)
        return True

    async def autonomous_execution(self, page):
        """Negotiation & Auto-Delivery Logic"""
        await page.goto("https://www.freelancer.com/messages")
        await asyncio.sleep(5)
        threads = await page.query_selector_all(self.selectors["chat_threads"])
        
        for thread in threads[:3]:
            await thread.click()
            await asyncio.sleep(3)
            messages = await page.query_selector_all(self.selectors["chat_messages"])
            if not messages: continue
            
            history = [await m.inner_text() for m in messages[-5:]]
            last_msg = history[-1]
            chat_id = str(hash(last_msg))

            if self.redis.get(f"done:{chat_id}"): continue

            # Milestone Funded ဖြစ်မဖြစ်စစ်ခြင်း
            is_funded = await page.query_selector(self.selectors["milestone_badge"]) is not None

            if is_funded:
                # PHASE: AUTO-DELIVERY
                prompt = f"Based on this chat: {history}, write only the production code solution."
                code = await self.get_ai_brain(prompt)
                if code:
                    await self.notify("💰 <b>Milestone Funded!</b> Delivering Code...")
                    await self.human_type(page.locator(self.selectors["message_box"]), "Project completed. Code attached.")
                    await page.click(self.selectors["send_msg_btn"])
                    self.redis.setex(f"done:{chat_id}", 2592000, "delivered")
            else:
                # PHASE: NEGOTIATION
                prompt = f"Client said: '{last_msg}'. Reply professionally to get the project awarded."
                reply = await self.get_ai_brain(prompt)
                if reply:
                    await self.human_type(page.locator(self.selectors["message_box"]), reply)
                    await page.click(self.selectors["send_msg_btn"])
                    self.redis.setex(f"done:{chat_id}", 86400, "replied")

    async def run(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--single-process"])
            context = await browser.new_context(user_agent="Mozilla/5.0...")
            page = await context.new_page()
            await stealth_async(page)
            
            # RAM Saving: Block Images & CSS
            await page.route("**/*", lambda r: r.abort() if r.request.resource_type in ["image", "font", "stylesheet"] else r.continue())

            await self.handle_login(page)
            while True:
                try:
                    await self.autonomous_execution(page)
                    gc.collect()
                except Exception as e: logger.error(f"Loop Error: {e}")
                await asyncio.sleep(1200)

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    asyncio.run(AutoIncomeGenerator().run())
