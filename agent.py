import os, json, asyncio, random, logging, redis, gc, threading, http.server, socketserver, requests
from datetime import datetime
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from poe_api_wrapper import PoeApi
from telegram import Bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("OmniAgent_Final")

PORT = int(os.environ.get("PORT", 10000))

# --- RENDER HEALTH SERVER ---
def run_health_server():
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args): pass
    with socketserver.TCPServer(("", PORT), QuietHandler) as httpd:
        httpd.serve_forever()

class AutoIncomeGenerator:
    def __init__(self):
        self.redis = redis.from_url(os.getenv("REDIS_URL"), decode_responses=True)
        self.bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
        self.user_id = os.getenv("TELEGRAM_USER_ID")
        self.ui = {
            "login_email": "input[type='email']",
            "login_pass": "input[type='password']",
            "chat_threads": "fl-message-thread-item",
            "chat_messages": ".fl-message-bubble-text",
            "message_box": "textarea[placeholder*='Type a message']",
            "send_msg_btn": "button[data-color='secondary']",
            "milestone_badge": ".Milestone-badge--funded"
        }

    async def notify(self, msg):
        try: await self.bot.send_message(self.user_id, msg, parse_mode='HTML')
        except: pass

    async def get_ai_brain(self, prompt, model="Claude-3.5-Sonnet"):
        """PB နှင့် PLAT Cookie နှစ်ခုလုံးကို အသုံးပြုထားသည်"""
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

    async def handle_negotiations_and_delivery(self, page):
        """Negotiation နှင့် Auto-Delivery logic"""
        logger.info("Scanning Inbox for Negotiations...")
        await page.goto("https://www.freelancer.com/messages", wait_until="domcontentloaded")
        await asyncio.sleep(8)
        threads = await page.query_selector_all(self.ui["chat_threads"])
        
        for thread in threads[:3]:
            await thread.click()
            await asyncio.sleep(4)
            messages = await page.query_selector_all(self.ui["chat_messages"])
            if not messages: continue
            
            history = [await m.inner_text() for m in messages[-5:]]
            last_msg = history[-1]
            chat_id = str(hash(last_msg))

            if self.redis.get(f"done:{chat_id}"): continue

            is_funded = await page.query_selector(self.ui["milestone_badge"]) is not None

            if is_funded:
                # AUTO-DELIVERY
                prompt = f"Based on: {history}, write the production Python code solution."
                code = await self.get_ai_brain(prompt)
                if code:
                    await self.notify("💰 <b>Milestone Funded!</b> Delivering Code...")
                    await self.human_type(page.locator(self.ui["message_box"]), "Completed. Solution attached.")
                    await page.click(self.ui["send_msg_btn"])
                    self.redis.setex(f"done:{chat_id}", 2592000, "delivered")
            else:
                # NEGOTIATION
                prompt = f"Client: '{last_msg}'. Reply to close the deal as Pho Go."
                reply = await self.get_ai_brain(prompt)
                if reply:
                    await self.human_type(page.locator(self.ui["message_box"]), reply)
                    await page.click(self.ui["send_msg_btn"])
                    self.redis.setex(f"done:{chat_id}", 86400, "replied")

    async def run(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--single-process"])
            page = await browser.new_page()
            await stealth_async(page)
            
            # Syntax Fixed: Async Resource Blocker
            async def block_resources(route):
                if route.request.resource_type in ["image", "font", "stylesheet"]:
                    await route.abort()
                else:
                    await route.continue()
            
            await page.route("**/*", block_resources)

            # Login Phase
            logger.info("Logging into Freelancer...")
            await page.goto("https://www.freelancer.com/login")
            await page.fill(self.ui["login_email"], os.getenv("FL_EMAIL"))
            await page.fill(self.ui["login_pass"], os.getenv("FL_PASSWORD"))
            await page.click("button[type='submit']")
            await asyncio.sleep(10)

            while True:
                try:
                    await self.handle_negotiations_and_delivery(page)
                    gc.collect()
                except Exception as e:
                    logger.error(f"Cycle Error: {e}")
                await asyncio.sleep(random.randint(1200, 2400))

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    asyncio.run(AutoIncomeGenerator().run())
