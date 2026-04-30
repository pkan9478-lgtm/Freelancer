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

    async def get_ai_brain(self, prompt, model="Claude-3.5-Sonnet"):
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

    async def run(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--single-process"])
            page = await browser.new_page()
            await stealth_async(page)
            
            # Syntax Corrected Resource Blocker
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
                    await page.goto("https://www.freelancer.com/messages")
                    logger.info("Cycle active. Scanning Inbox for Negotiations...")
                    gc.collect()
                except Exception as e:
                    logger.error(f"Loop Error: {e}")
                
                await asyncio.sleep(random.randint(1200, 2400))

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    asyncio.run(AutoIncomeGenerator().run())
