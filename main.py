import os
import hmac
import hashlib
import json
import threading
import datetime
import time
import requests
import base64
from urllib.parse import parse_qs
from fastapi import FastAPI, Depends, HTTPException, Request, Header, UploadFile, File, Form
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
import redis
from telebot import TeleBot, types

# ==========================================
# ၁။ CONFIGURATION & SETUP
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://your-render-app-url.onrender.com")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
ADMIN_TELEGRAM_ID = os.environ.get("ADMIN_TELEGRAM_ID", "YOUR_ID") 
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "") 

bot = TeleBot(BOT_TOKEN)
app = FastAPI(title="Digital Mall Auto-Run System Pro")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_headers=["*"], allow_methods=["*"])

# ==========================================
# ၂။ DATABASE MODELS
# ==========================================
DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR}/mall_ai_pro.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, index=True)
    full_name = Column(String)
    role = Column(String, default="buyer") 
    default_address = Column(String, default="") 
    phone = Column(String, default="")
    vendor_qr_file_id = Column(String, default="") # Vendor ၏ QR ပုံ
    vendor_location = Column(String, default="") # Auto Detect ဖြင့်ရသော လိပ်စာ

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    name = Column(String, index=True)
    price = Column(Float)
    description = Column(String, default="") 
    category = Column(String, default="General")
    image_file_id = Column(String, default="")
    stock = Column(Integer, default=10) 
    vendor_id = Column(Integer, ForeignKey("users.id")) 
    vendor = relationship("User")

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    product_id = Column(Integer, ForeignKey("products.id"))
    quantity = Column(Integer, default=1) 
    payment_method = Column(String, default="cod") # 'cod' or 'qr'
    transaction_screenshot = Column(String, default="") # ငွေလွှဲပြေစာပုံ
    address = Column(String) 
    status = Column(String, default="pending") 
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    product = relationship("Product")
    user = relationship("User")

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# ==========================================
# ၃။ SECURE AUTHENTICATION & UTILS
# ==========================================
def get_current_user(x_telegram_init_data: str = Header(None), db: Session = Depends(get_db)):
    if not x_telegram_init_data: raise HTTPException(status_code=401)
    try:
        vals = {k: v[0] for k, v in parse_qs(x_telegram_init_data).items()}
        hash_str = vals.pop('hash', None)
        data_check_str = "\n".join([f"{k}={v}" for k, v in sorted(vals.items())])
        secret_key = hmac.new("WebAppData".encode(), BOT_TOKEN.encode(), hashlib.sha256).digest()
        hmac_res = hmac.new(secret_key, data_check_str.encode(), hashlib.sha256).hexdigest()
        if hmac_res != hash_str: raise HTTPException(status_code=401)
        tg_user = json.loads(vals['user'])
    except: raise HTTPException(status_code=401)
    
    db_user = db.query(User).filter(User.telegram_id == str(tg_user['id'])).first()
    if not db_user:
        role = "admin" if str(tg_user['id']) == ADMIN_TELEGRAM_ID else "buyer"
        db_user = User(telegram_id=str(tg_user['id']), full_name=tg_user.get('first_name', 'User'), role=role)
        db.add(db_user)
        db.commit()
        db.refresh(db_user)
    return db_user

@app.get("/api/image/{file_id}")
def get_telegram_image(file_id: str):
    try:
        file_info = bot.get_file(file_id)
        res = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}")
        return Response(content=res.content, media_type="image/jpeg")
    except: raise HTTPException(status_code=404)

# ==========================================
# ၄။ API ENDPOINTS (Dynamic JSON & Core Logic)
# ==========================================
@app.get("/api/auth")
def authenticate_user(user: User = Depends(get_current_user)):
    return {
        "user": {
            "id": user.telegram_id, "name": user.full_name, "role": user.role, 
            "default_address": user.default_address, "phone": user.phone,
            "vendor_qr": user.vendor_qr_file_id, "vendor_location": user.vendor_location
        }
    }

# Update Vendor Profile (Location & QR)
@app.post("/api/vendor/profile")
async def update_vendor_profile(req: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await req.json()
    if data.get("location"): user.vendor_location = data.get("location")
    if data.get("qr_file_id"): user.vendor_qr_file_id = data.get("qr_file_id")
    user.role = "vendor" # Auto-upgrade
    db.commit()
    return {"status": "success"}

# Upload Image directly to Telegram via API (Memory Efficient)
@app.post("/api/upload_image")
async def upload_image(file: UploadFile = File(...), user: User = Depends(get_current_user)):
    try:
        content = await file.read()
        res = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", 
                            data={"chat_id": user.telegram_id}, files={"photo": content}).json()
        if res.get("ok"): return {"file_id": res["result"]["photo"][-1]["file_id"]}
        raise HTTPException(status_code=400, detail="Upload failed")
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/products")
def get_products(category: str = "All", search: str = "", skip: int = 0, limit: int = 20, db: Session = Depends(get_db)):
    query = db.query(Product)
    if category != "All": query = query.filter(Product.category == category)
    if search: query = query.filter(Product.name.ilike(f"%{search}%"))
    products = query.order_by(Product.id.desc()).offset(skip).limit(limit).all()
    res = []
    for p in products:
        vendor_qr = p.vendor.vendor_qr_file_id if p.vendor else ""
        res.append({"id":p.id, "name":p.name, "price":p.price, "desc":p.description, "category":p.category, "img":p.image_file_id, "stock":p.stock, "vendor_qr": vendor_qr})
    return {"products": res}

@app.post("/api/checkout")
async def checkout_cart(req: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await req.json()
    cart_items = data.get('cart', []) 
    address = data.get('address', 'Unknown')
    phone = data.get('phone', '')
    payment_method = data.get('payment_method', 'cod')
    tx_screenshot = data.get('transaction_screenshot', '')

    if not cart_items: raise HTTPException(status_code=400, detail="Cart is empty")
    
    total_amount = 0
    vendors_to_notify = set()
    ordered_names = []

    for item in cart_items:
        product = db.query(Product).filter(Product.id == item['id']).with_for_update().first()
        if product and product.stock >= item['qty']:
            db.add(Order(user_id=user.id, product_id=product.id, quantity=item['qty'], 
                         address=f"{address} (Ph: {phone})", payment_method=payment_method, 
                         transaction_screenshot=tx_screenshot))
            product.stock -= item['qty']
            total_amount += (product.price * item['qty'])
            ordered_names.append(f"{product.name} (x{item['qty']})")
            if product.vendor: vendors_to_notify.add((product.vendor.telegram_id, product.name, item['qty']))
        else:
            db.rollback()
            raise HTTPException(status_code=400, detail=f"'{product.name if product else 'Item'}' ပစ္စည်းလက်ကျန်မလုံလောက်ပါ။")
            
    user.default_address = address
    user.phone = phone
    db.commit()

    # Notify via Telegram
    items_str = "\n".join([f"- {n}" for n in ordered_names])
    pay_txt = "အိမ်ရောက်မှ ငွေချေမည် (COD)" if payment_method == 'cod' else "QR ဖြင့် ငွေပေးချေထားသည်"
    
    bot.send_message(user.telegram_id, f"🛒 **အော်ဒါ လက်ခံရရှိပါသည်**\n\n{items_str}\n\nစုစုပေါင်း: {total_amount:,.0f} Ks\nစနစ်: {pay_txt}", parse_mode="Markdown")
    
    for v_tg_id, p_name, qty in vendors_to_notify:
        msg = f"🔔 **အော်ဒါအသစ်ဝင်ပါသည်**\nဝယ်သူ: {user.full_name}\nပစ္စည်း: {p_name} (x{qty})\nစနစ်: {pay_txt}\nလိပ်စာ: {address} ({phone})"
        if payment_method != 'cod' and tx_screenshot:
            bot.send_photo(v_tg_id, tx_screenshot, caption=msg, parse_mode="Markdown")
        else:
            bot.send_message(v_tg_id, msg, parse_mode="Markdown")

    return {"status": "success"}

# Orders & Vendors API functions (Similar to previous, omitted for brevity but assumed present in production)
@app.get("/api/buyer/orders")
def get_buyer_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    orders = db.query(Order).filter(Order.user_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "qty": o.quantity, "price": o.product.price, "status": o.status, "date": o.created_at.strftime("%Y-%m-%d")} for o in orders]

# ==========================================
# ၅။ AI-POWERED CMS (Groq Integration)
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🏬 ကုန်တိုက်သို့ဝင်ရန်", web_app=types.WebAppInfo(WEBAPP_URL)))
    bot.send_message(message.chat.id, "မင်္ဂလာပါရှင်။\nApp သို့ဝင်ရောက်၍ ဈေးဝယ်နိုင်သလို၊ ရောင်းချလိုပါကလည်း App ထဲမှ 'ရောင်းမည်' ကိုနှိပ်၍ စတင်နိုင်ပါသည်။", reply_markup=markup)

@bot.message_handler(content_types=['photo'])
def handle_cms_photo(message):
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == str(message.from_user.id)).first()
    if not user or user.role not in ["vendor", "admin"]: return db.close()

    try:
        caption = message.caption or "New Product"
        file_id = message.photo[-1].file_id 
        ai_data = {"name": caption[:20], "price": 0, "category": "General", "description": caption, "stock": 10}
        
        if GROQ_API_KEY:
            try:
                msg = bot.reply_to(message, "⏳ AI ဖြင့် ခွဲခြမ်းစိတ်ဖြာနေပါသည်...")
                headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
                prompt = f"""Extract JSON from this Burmese e-commerce text: "{caption}". Keys: 'name', 'price' (number), 'category', 'description', 'stock' (number)."""
                res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}).json()
                parsed = json.loads(res['choices'][0]['message']['content'])
                for k in ai_data.keys():
                    if parsed.get(k): ai_data[k] = parsed[k]
                bot.delete_message(message.chat.id, msg.message_id)
            except: pass

        db.add(Product(name=ai_data['name'], price=float(ai_data['price']), description=ai_data['description'], category=ai_data['category'], stock=int(ai_data['stock']), image_file_id=file_id, vendor_id=user.id))
        db.commit()
        bot.reply_to(message, f"✅ **ပစ္စည်းတင်ပြီးပါပြီ။**\n\nအမည်: {ai_data['name']}\nဈေးနှုန်း: {ai_data['price']} Ks", parse_mode="Markdown")
    except: bot.reply_to(message, "အမှားအယွင်း ဖြစ်ပေါ်ခဲ့ပါသည်။")
    finally: db.close()

# ==========================================
# ၆။ FRONTEND UI (Full Version)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    return """
    <!DOCTYPE html>
    <html lang="my">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <script src="https://cdn.tailwindcss.com"></script>
        <title>Digital Mall</title>
        <style>
            body { font-family: sans-serif; background-color: #f3f4f6; -webkit-tap-highlight-color: transparent; }
            .tab-btn.active { color: #2563eb; border-bottom: 3px solid #2563eb; }
            #toast { visibility: hidden; min-width: 250px; background-color: rgba(31, 41, 55, 0.95); color: #fff; text-align: center; border-radius: 12px; padding: 14px; position: fixed; z-index: 9999; left: 50%; bottom: 80px; transform: translateX(-50%); font-size: 14px; box-shadow: 0 10px 15px rgba(0,0,0,0.1); }
            #toast.show { visibility: visible; animation: fadein 0.3s, fadeout 0.3s 2.5s; }
            @keyframes fadein { from {bottom: 50px; opacity: 0;} to {bottom: 80px; opacity: 1;} }
            @keyframes fadeout { from {bottom: 80px; opacity: 1;} to {bottom: 50px; opacity: 0;} }
            .modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 1000; display: flex; align-items: center; justify-content: center; backdrop-filter: blur(4px); }
        </style>
    </head>
    <body class="pb-20">
        <header class="bg-white p-4 shadow-sm sticky top-0 z-40 flex justify-between items-center">
            <span class="font-bold text-blue-700 text-xl tracking-tight">Digital<span class="text-gray-800">Mall</span></span>
            <button onclick="showTab('cart-tab', 'btn-shop')" class="relative p-2.5 rounded-full bg-blue-50 text-blue-600">
                🛒 <span id="cart-count" class="absolute -top-1 -right-1 bg-red-500 text-white text-[10px] rounded-full px-1.5 hidden">0</span>
            </button>
        </header>

        <div class="fixed bottom-0 w-full bg-white border-t flex justify-around text-xs font-medium text-gray-500 z-50 shadow-lg">
            <button id="btn-shop" onclick="showTab('shop-tab', 'btn-shop')" class="tab-btn active flex-1 py-3 flex flex-col items-center">🏠<span>ဝယ်မည်</span></button>
            <button onclick="openVendorProfile()" class="tab-btn flex-1 py-3 flex flex-col items-center relative">
                <div class="absolute -top-4 bg-blue-600 text-white w-12 h-12 rounded-full flex items-center justify-center shadow-lg border-4 border-white text-2xl pb-1">+</div>
                <span class="mt-5 font-bold text-blue-600">ရောင်းမည်</span>
            </button>
            <button id="btn-history" onclick="showTab('history-tab', 'btn-history')" class="tab-btn flex-1 py-3 flex flex-col items-center">📋<span>မှတ်တမ်း</span></button>
        </div>

        <div id="vendor-modal" class="modal-overlay hidden">
            <div class="bg-white w-[90%] max-w-md p-6 rounded-2xl shadow-xl">
                <h2 class="text-xl font-bold mb-4 text-gray-800">ရောင်းချသူ အချက်အလက်ဖြည့်ရန်</h2>
                <p class="text-xs text-gray-500 mb-4">သင်၏ တည်နေရာနှင့် ငွေလွှဲလက်ခံမည့် QR ကို အလွယ်တကူ ထည့်သွင်းပါ။</p>
                
                <div class="mb-4">
                    <label class="block text-sm font-bold text-gray-700 mb-2">၁။ ဆိုင်တည်နေရာ (Location)</label>
                    <div class="flex gap-2">
                        <textarea id="v-location" class="w-full p-2 border rounded-lg text-sm bg-gray-50" rows="2" placeholder="လိပ်စာ အလိုအလျောက် ပေါ်လာပါမည်"></textarea>
                        <button onclick="autoDetectLocation()" class="bg-blue-100 text-blue-600 p-2 rounded-lg font-bold text-2xl flex items-center justify-center">📍</button>
                    </div>
                </div>

                <div class="mb-4">
                    <label class="block text-sm font-bold text-gray-700 mb-2">၂။ KPay / Wave QR Code (Optional)</label>
                    <input type="file" id="v-qr-upload" accept="image/*" class="w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded-full file:border-0 file:text-sm file:font-semibold file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100"/>
                    <input type="hidden" id="v-qr-file-id">
                </div>

                <button onclick="saveVendorProfile()" class="w-full bg-blue-600 text-white py-3 rounded-xl font-bold mt-2">ဆက်လုပ်မည်</button>
                <button onclick="closeVendorProfile()" class="w-full bg-gray-100 text-gray-600 py-3 rounded-xl font-bold mt-2">ပိတ်မည်</button>
            </div>
        </div>

        <div id="shop-tab" class="tab-content p-4">
            <div id="product-list" class="grid grid-cols-2 gap-4"></div>
        </div>

        <div id="cart-tab" class="tab-content hidden p-4">
            <h2 class="font-bold text-gray-800 text-xl mb-4">🛒 ခြင်းတောင်း</h2>
            <div id="cart-items" class="space-y-3 mb-4"></div>
            
            <div class="bg-white p-5 rounded-2xl shadow-sm border mb-4">
                <h3 class="font-bold mb-3 text-sm">ပို့ဆောင်ရမည့် လိပ်စာ</h3>
                <input type="text" id="c-address" placeholder="အိမ်အမှတ်၊ လမ်း၊ မြို့နယ်" class="w-full p-3 mb-3 border rounded-lg text-sm bg-gray-50">
                <input type="tel" id="c-phone" placeholder="09xxxxxxxxx" class="w-full p-3 mb-4 border rounded-lg text-sm bg-gray-50">
                
                <h3 class="font-bold mb-3 text-sm">ငွေပေးချေမှု စနစ်</h3>
                <div class="space-y-2 mb-4">
                    <label class="flex items-center gap-2 p-3 border rounded-xl bg-blue-50 cursor-pointer">
                        <input type="radio" name="payment_type" value="cod" checked onchange="togglePaymentUI()">
                        <span class="font-bold text-sm text-gray-800">🚚 အိမ်ရောက်မှ ငွေချေမည် (COD)</span>
                    </label>
                    <label class="flex items-center gap-2 p-3 border rounded-xl cursor-pointer">
                        <input type="radio" name="payment_type" value="qr" onchange="togglePaymentUI()">
                        <span class="font-bold text-sm text-gray-800">📱 QR Scan ဖြင့် ငွေလွှဲမည်</span>
                    </label>
                </div>

                <div id="qr-payment-section" class="hidden bg-gray-50 p-4 rounded-xl mb-4 border">
                    <p class="text-xs text-gray-500 mb-2">ရောင်းချသူ၏ QR ကို စကင်ဖတ်၍ ငွေလွှဲပါ၊ ထို့နောက် Screenshot ကို Upload တင်ပေးပါ။</p>
                    <img id="vendor-qr-img" src="" class="w-full max-w-[200px] mx-auto rounded-lg mb-3 shadow hidden">
                    <input type="file" id="c-receipt-upload" accept="image/*" class="w-full text-sm file:bg-blue-50 file:text-blue-700 file:border-0 file:rounded-full file:px-3 file:py-1">
                    <input type="hidden" id="c-receipt-file-id">
                </div>

                <div class="flex justify-between font-bold text-lg mb-4"><span>စုစုပေါင်း:</span><span id="cart-total" class="text-blue-600">0 Ks</span></div>
                <button onclick="checkoutCart()" class="w-full bg-blue-600 text-white py-4 rounded-xl font-bold">အော်ဒါတင်မည်</button>
            </div>
        </div>

        <div id="history-tab" class="tab-content hidden p-4"><div id="buyer-order-list" class="space-y-3"></div></div>
        <div id="toast">Message</div>

        <script>
            const tg = window.Telegram.WebApp;
            const initData = tg.initData;
            let cart = [];

            async function apiFetch(url, options = {}) {
                return fetch(url, { ...options, headers: { 'X-Telegram-Init-Data': initData, ...options.headers }});
            }

            function showToast(msg) {
                const t = document.getElementById("toast");
                t.innerText = msg; t.className = "show";
                if(tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
                setTimeout(() => { t.className = t.className.replace("show", ""); }, 2800);
            }

            function showTab(tabId, btnId) {
                document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
                document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
                document.getElementById(tabId).classList.remove('hidden');
                if(btnId) document.getElementById(btnId).classList.add('active');
                if(tabId === 'history-tab') loadOrders();
                if(tabId === 'cart-tab') renderCart();
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }

            // =====================================
            // VENDOR ONBOARDING (LOCATION + QR)
            // =====================================
            function openVendorProfile() { document.getElementById('vendor-modal').classList.remove('hidden'); }
            function closeVendorProfile() { document.getElementById('vendor-modal').classList.add('hidden'); }

            function autoDetectLocation() {
                if(!navigator.geolocation) return showToast("သင့်ဖုန်းတွင် Location စနစ် မရနိုင်ပါ။");
                showToast("တည်နေရာကို ရှာဖွေနေပါသည်...");
                navigator.geolocation.getCurrentPosition(async (pos) => {
                    const lat = pos.coords.latitude; const lon = pos.coords.longitude;
                    try {
                        const res = await fetch(`https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lon}&format=json`);
                        const data = await res.json();
                        document.getElementById('v-location').value = data.display_name;
                        showToast("တည်နေရာ ရရှိပါပြီ");
                    } catch(e) { showToast("လိပ်စာပြောင်းလဲခြင်း မအောင်မြင်ပါ။"); }
                }, () => showToast("Location ဖွင့်ပေးရန် လိုအပ်ပါသည်။"));
            }

            async function uploadImageNative(fileInputId, hiddenInputId) {
                const file = document.getElementById(fileInputId).files[0];
                if(!file) return null;
                const formData = new FormData(); formData.append("file", file);
                const res = await fetch('/api/upload_image', { method: 'POST', body: formData, headers: {'X-Telegram-Init-Data': initData} });
                if(res.ok) { const data = await res.json(); document.getElementById(hiddenInputId).value = data.file_id; return data.file_id; }
                return null;
            }

            async function saveVendorProfile() {
                tg.MainButton.showProgress();
                let qrFileId = "";
                if(document.getElementById('v-qr-upload').files.length > 0) {
                    qrFileId = await uploadImageNative('v-qr-upload', 'v-qr-file-id');
                }
                const payload = { location: document.getElementById('v-location').value, qr_file_id: qrFileId };
                await apiFetch('/api/vendor/profile', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
                tg.MainButton.hideProgress();
                closeVendorProfile();
                
                tg.showConfirm("အောင်မြင်ပါသည်။ ယခု Bot Chat သို့သွားပြီး ရောင်းချမည့် ပစ္စည်းပုံနှင့် ဈေးနှုန်းကို Message ပို့လိုက်ပါ။ ပို့မည်လား?", (r) => { if(r) tg.close(); });
            }

            // =====================================
            // PRODUCTS & CART LOGIC
            // =====================================
            async function loadProducts() {
                const res = await apiFetch(`/api/products`);
                const data = await res.json();
                document.getElementById('product-list').innerHTML = data.products.map(p => `
                    <div class="bg-white rounded-xl shadow-sm border p-3 flex flex-col">
                        <img src="${p.img ? '/api/image/'+p.img : 'https://via.placeholder.com/150'}" class="w-full h-32 object-cover rounded mb-2">
                        <div class="font-bold text-sm mb-1">${p.name}</div>
                        <div class="text-blue-600 font-bold text-xs mb-2">${p.price.toLocaleString()} Ks</div>
                        <button onclick="addToCart(${p.id}, '${p.name}', ${p.price}, '${p.vendor_qr}')" class="mt-auto bg-blue-50 text-blue-600 py-1.5 rounded font-bold text-xs">ဝယ်မည်</button>
                    </div>`).join('');
            }

            function addToCart(id, name, price, vendorQr) {
                let existing = cart.find(i => i.id === id);
                if(existing) existing.qty++; else cart.push({id, name, price, qty: 1, vendorQr});
                updateCartBadge(); showToast("ခြင်းထဲရောက်ပါပြီ");
            }

            function updateCartBadge() {
                const b = document.getElementById('cart-count');
                let t = cart.reduce((s, i) => s + i.qty, 0);
                b.innerText = t; t > 0 ? b.classList.remove('hidden') : b.classList.add('hidden');
            }

            function renderCart() {
                let total = 0;
                document.getElementById('cart-items').innerHTML = cart.map((i, idx) => {
                    total += (i.price * i.qty);
                    return `<div class="bg-white p-3 rounded-xl border flex justify-between items-center mb-2">
                        <div><div class="text-sm font-bold">${i.name}</div><div class="text-blue-600 text-xs">${i.price} Ks</div></div>
                        <div class="flex gap-2 items-center bg-gray-100 rounded px-2"><button onclick="cart[${idx}].qty--; renderCart()">-</button><span>${i.qty}</span><button onclick="cart[${idx}].qty++; renderCart()">+</button></div>
                    </div>`;
                }).join('');
                document.getElementById('cart-total').innerText = `${total.toLocaleString()} Ks`;
                togglePaymentUI();
            }

            function togglePaymentUI() {
                const type = document.querySelector('input[name="payment_type"]:checked').value;
                const qrSec = document.getElementById('qr-payment-section');
                if(type === 'qr') {
                    qrSec.classList.remove('hidden');
                    if(cart.length > 0 && cart[0].vendorQr) {
                        const img = document.getElementById('vendor-qr-img');
                        img.src = '/api/image/' + cart[0].vendorQr; img.classList.remove('hidden');
                    }
                } else { qrSec.classList.add('hidden'); }
            }

            async function checkoutCart() {
                if(cart.length === 0) return;
                const addr = document.getElementById('c-address').value;
                const phone = document.getElementById('c-phone').value;
                const payType = document.querySelector('input[name="payment_type"]:checked').value;
                
                if(!addr || !phone) return showToast("လိပ်စာ နှင့် ဖုန်းနံပါတ် ထည့်ပါ။");

                let receiptId = "";
                if(payType === 'qr') {
                    receiptId = await uploadImageNative('c-receipt-upload', 'c-receipt-file-id');
                    if(!receiptId) return showToast("ငွေလွှဲပြေစာ Screenshot တင်ပေးပါ။");
                }

                tg.MainButton.showProgress();
                const payload = { address: addr, phone: phone, payment_method: payType, transaction_screenshot: receiptId, cart: cart.map(i=>({id:i.id, qty:i.qty})) };
                
                const res = await apiFetch(`/api/checkout`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
                if(res.ok) { cart=[]; updateCartBadge(); showToast("အော်ဒါတင်ပြီးပါပြီ"); showTab('history-tab', 'btn-history'); }
                tg.MainButton.hideProgress();
            }

            async function loadOrders() {
                const res = await apiFetch('/api/buyer/orders'); const data = await res.json();
                document.getElementById('buyer-order-list').innerHTML = data.map(o => `
                    <div class="bg-white p-3 rounded-xl border mb-2"><div class="font-bold text-sm">${o.name} (x${o.qty})</div><div class="text-xs text-gray-500">${o.status}</div></div>
                `).join('');
            }

            window.onload = () => { tg.expand(); tg.ready(); loadProducts(); };
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn
    try: bot.remove_webhook(); time.sleep(1) 
    except: pass
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
