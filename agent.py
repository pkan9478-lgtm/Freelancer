import os, json, asyncio, random, logging, redis, gc, threading, http.server, socketserver, requests
from datetime import datetime
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from poe_api_wrapper import PoeApi
from telegram import Bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("PhoGo_Ultra_Gen")

PORT = int(os.environ.get("PORT", 10000))

# --- RENDER HEALTH & PERSISTENCE ---
def run_health_server():
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args): pass
    with socketserver.TCPServer(("", PORT), QuietHandler) as httpd:
        httpd.serve_forever()

def self_ping():
    url = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")
    import time
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
            "chat_threads": "fl-message-thread-item",
            "chat_messages": "fl-message-bubble-text",
            "message_box": "textarea[placeholder*='Type a message']",
            "file_upload": "input[type='file']",
            "send_msg_btn": "button[data-color='secondary']",
            "milestone_badge": ".Milestone-badge--funded"
        }

    async def notify(self, msg):
        try: await self.bot.send_message(self.user_id, msg, parse_mode='HTML')
        except: pass

    async def get_ai_brain(self, prompt, model="Claude-3.5-Sonnet"):
        """အဆင့်မြင့် POE_PLAT_COOKIE ကို အသုံးပြုထားသော AI Engine"""
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

    def extract_code_to_buffer(self, ai_output, file_prefix):
        data = ai_output
        if "```" in ai_output:
            parts = ai_output.split("```")
            if len(parts) > 1: data = parts[1].split("\n", 1)[-1] 
        file_name = f"{file_prefix}_{int(datetime.now().timestamp())}.py"
        return {"name": file_name, "mimeType": "text/x-python", "buffer": data.strip().encode('utf-8')}

    async def human_type(self, element, text):
        await element.fill("")
        await element.type(text, delay=random.randint(30, 80))

    async def handle_login(self, page):
        logger.info("Logging into Freelancer...")
        await page.goto("https://www.freelancer.com/login")
        await asyncio.sleep(5)
        await self.human_type(page.locator(self.ui["login_email"]), os.getenv("FL_EMAIL"))
        await self.human_type(page.locator(self.ui["login_pass"]), os.getenv("FL_PASSWORD"))
        await page.click("button[type='submit']")
        await asyncio.sleep(12) 
        return True

    async def handle_negotiations_and_delivery(self, page):
        """Negotiation နှင့် Auto-Delivery အပိုင်း"""
        logger.info("Scanning Inbox...")
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
                # AUTO-DELIVERY PHASE
                prompt = f"Based on: {history}, write only the final Python code solution."
                code = await self.get_ai_brain(prompt)
                if code:
                    await self.notify("💰 <b>Milestone Funded!</b> Sending Solution...")
                    await self.human_type(page.locator(self.ui["message_box"]), "I've finished the task. Code attached.")
                    await page.click(self.ui["send_msg_btn"])
                    self.redis.setex(f"done:{chat_id}", 2592000, "delivered")
            else:
                # NEGOTIATION PHASE
                prompt = f"Client said: '{last_msg}'. Reply to get the award."
                reply = await self.get_ai_brain(prompt)
                if reply:
                    await self.human_type(page.locator(self.ui["message_box"]), reply)
                    await page.click(self.ui["send_msg_btn"])
                    self.redis.setex(f"done:{chat_id}", 86400, "replied")

    async def run_core(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--single-process"])
            context = await browser.new_context(viewport={'width': 1280, 'height': 720})
            page = await context.new_page()
            await stealth_async(page)

            # RAM Saving: Async Resource Blocker (Syntax Corrected)
            async def block_resources(route):
                if route.request.resource_type in ["image", "font", "stylesheet"]:
                    await route.abort()
                else: await route.continue()

            await page.route("**/*", block_resources)

            try:
                await self.handle_login(page)
                while True:
                    await self.handle_negotiations_and_delivery(page)
                    gc.collect()
                    logger.info("Cycle completed. Sleeping...")
                    await asyncio.sleep(random.randint(1800, 3600))
            except Exception as e:
                logger.error(f"Critical Error: {e}")
            finally:
                await browser.close()

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    asyncio.run(AutoIncomeGenerator().run_core())
