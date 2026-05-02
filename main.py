import os
import hmac
import hashlib
import json
import threading
import datetime
import requests
from urllib.parse import parse_qs
from fastapi import FastAPI, Depends, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
import redis
from telebot import TeleBot, types

# ==========================================
# ၁။ CONFIGURATION
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://your-render-app-url.onrender.com")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
ADMIN_TELEGRAM_ID = os.environ.get("ADMIN_TELEGRAM_ID", "YOUR_ID") 
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "") 

bot = TeleBot(BOT_TOKEN)
app = FastAPI(title="Digital Mall Pro")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_headers=["*"], allow_methods=["*"])

redis_client = None
try:
    if REDIS_URL:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        print("✅ Redis Connected")
except: 
    print("⚠️ Redis Not Connected")
    redis_client = None

# ==========================================
# ၂။ DATABASE MODELS
# ==========================================
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/mall_ai_pro.db")
os.makedirs(os.path.dirname(DATABASE_URL.replace("sqlite:///", "")), exist_ok=True)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, index=True)
    full_name = Column(String)
    role = Column(String, default="buyer") 

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
    transaction_id = Column(String) 
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
# ၃။ SECURE AUTHENTICATION
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
# ၄။ API ENDPOINTS 
# ==========================================
@app.get("/api/auth")
def authenticate_user(user: User = Depends(get_current_user)):
    return {"user": {"id": user.telegram_id, "name": user.full_name, "role": user.role}}

@app.get("/api/products")
def get_products(category: str = "All", skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    cache_key = f"ai_prods_render_{category}_{skip}_{limit}"
    if redis_client and (cached := redis_client.get(cache_key)): return json.loads(cached)
    
    query = db.query(Product)
    if category != "All": query = query.filter(Product.category == category)
    products = query.order_by(Product.id.desc()).offset(skip).limit(limit).all()
    
    res = [{"id":p.id, "name":p.name, "price":p.price, "desc":p.description, "category":p.category, "img":p.image_file_id, "stock":p.stock} for p in products]
    if redis_client: redis_client.setex(cache_key, 300, json.dumps(res)) 
    return res

@app.post("/api/checkout")
async def checkout_cart(req: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await req.json()
    cart_items = data.get('cart', []) 
    tx_id = data.get('transaction_id', 'Unknown')
    address = data.get('address', 'Unknown')

    if not cart_items: raise HTTPException(status_code=400, detail="Cart empty")
    total_amount, ordered_names = 0, []
    vendors_to_notify = set()

    for p_id in cart_items:
        product = db.query(Product).filter(Product.id == p_id).first()
        if product and product.stock > 0:
            db.add(Order(user_id=user.id, product_id=product.id, transaction_id=tx_id, address=address))
            product.stock -= 1 
            total_amount += product.price
            ordered_names.append(product.name)
            if product.vendor: vendors_to_notify.add((product.vendor.telegram_id, product.name))
        else:
            db.rollback()
            raise HTTPException(status_code=400, detail=f"{product.name if product else 'Item'} is out of stock.")
            
    db.commit()
    if redis_client:
        for key in redis_client.scan_iter("ai_prods_render_*"): redis_client.delete(key)

    try:
        items_str = "\n".join([f"- {n}" for n in ordered_names])
        bot.send_message(user.telegram_id, f"🛒 **အော်ဒါ လက်ခံရရှိပါသည်**\n\n{items_str}\n\nစုစုပေါင်း: {total_amount} Ks\nလိပ်စာ: {address}\nTx ID: `{tx_id}`", parse_mode="Markdown")
        for v_tg_id, p_name in vendors_to_notify:
            bot.send_message(v_tg_id, f"🔔 **အော်ဒါအသစ်**\nဝယ်သူ: {user.full_name}\nပစ္စည်း: {p_name}\nလိပ်စာ/ဖုန်း: {address}\nTx ID: `{tx_id}`", parse_mode="Markdown")
    except: pass
    return {"status": "success"}

@app.get("/api/buyer/orders")
def get_buyer_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    orders = db.query(Order).filter(Order.user_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "price": o.product.price, "status": o.status} for o in orders]

@app.get("/api/vendor/dashboard")
def get_vendor_dashboard(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    orders = db.query(Order).join(Product).filter(Product.vendor_id == user.id).all()
    total_sales = sum(o.product.price for o in orders if o.status in ["approved", "shipped", "delivered"])
    pending_count = sum(1 for o in orders if o.status == "pending")
    return {"total_revenue": total_sales, "total_orders": len(orders), "pending_orders": pending_count}

@app.get("/api/vendor/orders")
def get_vendor_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    orders = db.query(Order).join(Product).filter(Product.vendor_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "buyer": o.user.full_name, "tx": o.transaction_id, "addr": o.address, "status": o.status} for o in orders]

@app.post("/api/vendor/orders/{order_id}/status")
def update_order_status(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    status_map = {"approved": "✅ ငွေလွှဲမှန်ကန်ပါသည်။", "shipped": "🚚 ပစ္စည်းပို့ဆောင်ပေးလိုက်ပါပြီ။", "delivered": "🎁 ပစ္စည်းလက်ခံရရှိကြောင်း မှတ်တမ်းတင်ပြီးပါပြီ။"}
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or (order.product.vendor_id != user.id and user.role != "admin"): raise HTTPException(status_code=400)
    
    new_status = request.query_params.get("status", "shipped")
    order.status = new_status
    db.commit()
    try: 
        if new_status in status_map:
            bot.send_message(order.user.telegram_id, f"{status_map[new_status]}\nပစ္စည်း: **{order.product.name}**", parse_mode="Markdown")
    except: pass
    return {"status": "success"}

# ==========================================
# ၅။ AI-POWERED CMS BOT (Fixed Logic)
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🏬 ကုန်တိုက်သို့ဝင်ရန်", web_app=types.WebAppInfo(WEBAPP_URL)))
    bot.send_message(message.chat.id, f"ID: `{message.from_user.id}`\n\nDigital Mall မှ ကြိုဆိုပါတယ်။", parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(content_types=['photo'])
def handle_cms_photo(message):
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == str(message.from_user.id)).first()
    if not user or user.role not in ["vendor", "admin"]: return db.close()

    try:
        caption = message.caption or "ပစ္စည်းအသစ်"
        file_id = message.photo[-1].file_id 

        ai_data = {"name": "New Product", "price": 0, "category": "General", "description": caption, "stock": 10}
        
        # 🌟 AI Parsing 
        if GROQ_API_KEY:
            try:
                msg = bot.reply_to(message, "⏳ ဒေတာများကို စစ်ဆေးနေပါသည်...")
                headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
                prompt = f"""
                Analyze Burmese caption: "{caption}"
                Return strictly JSON: 'name' (short), 'price' (number in Kyats or 0), 'category' (Electronics/Fashion/Food/General), 'description' (1 promo sentence), 'stock' (number or 10).
                """
                payload = {"model": "llama3-8b-8192", "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}
                res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload).json()
                parsed = json.loads(res['choices'][0]['message']['content'])
                
                # အကယ်၍ AI မှ ဖြတ်ယူနိုင်သော အမည်ဖြစ်ပါကသာ ထည့်သွင်းမည်။
                if parsed.get('name') and parsed.get('name') != 'New Product': ai_data['name'] = parsed['name']
                if parsed.get('price'): ai_data['price'] = parsed['price']
                if parsed.get('category'): ai_data['category'] = parsed['category']
                if parsed.get('description'): ai_data['description'] = parsed['description']
                if parsed.get('stock'): ai_data['stock'] = parsed['stock']
                
                bot.delete_message(message.chat.id, msg.message_id)
            except: pass

        # 🌟 HIGHEST PRIORITY: သင်ကိုယ်တိုင် (-) ဖြင့် ရိုက်ထည့်လိုက်ပါက AI မှပေးသော အမည်နှင့် ဈေးနှုန်းကို ဖျက်ပြီး သင့်စာသားကိုသာ အသုံးပြုမည်။
        if "-" in caption:
            parts = caption.split("-")
            ai_data["name"] = parts[0].strip() # အမည်ကို အတိအကျ ယူမည်
            try:
                # ဈေးနှုန်းတွင် ပါလာနိုင်သော Ks, ks, ကော်မာ များကို ရှင်းလင်းမည်
                clean_price = parts[1].strip().lower().replace("ks", "").replace(",", "").strip()
                ai_data["price"] = float(clean_price)
            except: pass

        new_product = Product(
            name=ai_data['name'], price=float(ai_data['price']), 
            description=ai_data['description'], category=ai_data['category'], 
            stock=int(ai_data['stock']), image_file_id=file_id, vendor_id=user.id
        )
        db.add(new_product)
        db.commit()
        
        if redis_client:
            for key in redis_client.scan_iter("ai_prods_render_*"): redis_client.delete(key)
            
        bot.reply_to(message, f"✅ ပစ္စည်းတင်ပြီးပါပြီ။\n\n📌 အမည်: {ai_data['name']}\n💰 ဈေးနှုန်း: {ai_data['price']} Ks\n📦 လက်ကျန်: {ai_data['stock']}")
    except Exception as e: bot.reply_to(message, f"အမှားအယွင်း ဖြစ်ပေါ်ခဲ့ပါသည်။ ({str(e)})")
    finally: db.close()

# ==========================================
# ၆။ FRONTEND UI
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    return """
    <!DOCTYPE html>
    <html lang="my">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <script src="https://cdn.tailwindcss.com"></script>
        <title>Digital Mall Pro</title>
        <style>
            .tab-btn.active { color: #2563eb; border-bottom: 2px solid #2563eb; transition: 0.2s; }
            .cat-chip.active { background-color: #2563eb; color: white; border-color: #2563eb; }
            body { font-family: sans-serif; -webkit-tap-highlight-color: transparent; }
            .cart-badge { position: absolute; top: 5px; right: 10px; background: #ef4444; color: white; border-radius: 50%; padding: 2px 6px; font-size: 10px; font-weight: bold; }
            
            /* 🌟 IMAGE FIX: ပုံကို ဖိနှိပ်ပါက Menu များ မပေါ်လာစေရန် */
            img.product-img {
                -webkit-touch-callout: none; /* Disable iOS/Android context menu */
                -webkit-user-select: none;
                user-select: none;
                pointer-events: none; /* Make image completely unclickable */
            }

            #toast { visibility: hidden; min-width: 250px; background-color: #333; color: #fff; text-align: center; border-radius: 8px; padding: 12px; position: fixed; z-index: 50; left: 50%; bottom: 30px; transform: translateX(-50%); font-size: 14px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
            #toast.show { visibility: visible; animation: fadein 0.5s, fadeout 0.5s 2.5s; }
            @keyframes fadein { from {bottom: 0; opacity: 0;} to {bottom: 30px; opacity: 1;} }
            @keyframes fadeout { from {bottom: 30px; opacity: 1;} to {bottom: 0; opacity: 0;} }
        </style>
    </head>
    <body class="bg-gray-50 min-h-screen pb-24">
        
        <header class="bg-white p-4 shadow-sm sticky top-0 z-40 flex justify-between items-center">
            <span class="font-bold text-blue-600 text-lg">Digital Mall</span>
            <div class="flex gap-2">
                <button onclick="showTab('cart-tab', 'btn-shop')" class="relative bg-gray-100 p-2 rounded-full">
                    🛒 <span id="cart-count" class="cart-badge hidden">0</span>
                </button>
                <div class="text-xs bg-blue-50 px-3 py-2 rounded-full text-blue-700 font-semibold" id="display-name">Loading...</div>
            </div>
        </header>

        <div id="nav-bar" class="flex justify-around bg-white border-b text-sm font-bold text-gray-400">
            <button id="btn-shop" onclick="showTab('shop-tab', 'btn-shop')" class="tab-btn active w-full py-3">ဈေးဝယ်ရန်</button>
            <button id="btn-history" onclick="showTab('history-tab', 'btn-history')" class="tab-btn w-full py-3">မှတ်တမ်း</button>
            <button id="btn-orders" onclick="showTab('orders-tab', 'btn-orders')" class="tab-btn hidden w-full py-3">ဆိုင်ရှင်</button>
        </div>

        <div id="shop-tab" class="tab-content">
            <div class="px-4 pt-4">
                <input type="text" id="search-box" onkeyup="searchProducts()" onkeydown="checkEnter(event)" placeholder="🔍 ပစ္စည်းရှာရန်..." class="w-full p-2.5 bg-white shadow-sm rounded-xl border border-gray-200 text-sm focus:ring-1 focus:ring-blue-500 outline-none mb-3">
                <div class="flex gap-2 overflow-x-auto pb-2 scrollbar-hide">
                    <button onclick="filterCategory('All')" class="cat-chip active whitespace-nowrap px-4 py-1.5 rounded-full border border-gray-300 text-xs font-bold text-gray-600">အားလုံး</button>
                    <button onclick="filterCategory('Electronics')" class="cat-chip whitespace-nowrap px-4 py-1.5 rounded-full border border-gray-300 text-xs font-bold text-gray-600">အီလက်ထရောနစ်</button>
                    <button onclick="filterCategory('Fashion')" class="cat-chip whitespace-nowrap px-4 py-1.5 rounded-full border border-gray-300 text-xs font-bold text-gray-600">ဖက်ရှင်</button>
                    <button onclick="filterCategory('Food')" class="cat-chip whitespace-nowrap px-4 py-1.5 rounded-full border border-gray-300 text-xs font-bold text-gray-600">စားသောက်ကုန်</button>
                </div>
            </div>
            <div id="loader" class="text-center mt-10 text-blue-500 font-bold text-sm">🔄 ဒေတာဆွဲယူနေပါသည်...</div>
            <div id="product-list" class="p-4 grid grid-cols-2 gap-4"></div>
        </div>

        <div id="cart-tab" class="tab-content hidden p-4">
            <h2 class="font-bold mb-4 text-gray-700 text-lg">🛒 သင့်ခြင်းတောင်း</h2>
            <div id="cart-items" class="space-y-3 mb-6"></div>
            <div class="bg-white p-4 rounded-xl shadow-sm border border-gray-200">
                <div class="flex justify-between font-bold text-lg mb-4"><span>စုစုပေါင်း:</span> <span id="cart-total" class="text-blue-600">0 Ks</span></div>
                <textarea id="checkout-address" placeholder="ပို့ဆောင်ရမည့် လိပ်စာ နှင့် ဖုန်းနံပါတ်..." class="w-full p-2.5 mb-3 bg-gray-50 rounded-lg border text-sm" rows="2"></textarea>
                <input type="text" id="checkout-tx" placeholder="KPay/Wave Tx ID..." class="w-full p-2.5 mb-4 bg-gray-50 rounded-lg border text-sm">
                <button onclick="checkoutCart()" class="w-full bg-blue-600 text-white py-3 rounded-xl font-bold">ငွေချေပြီး အော်ဒါတင်မည်</button>
            </div>
        </div>

        <div id="history-tab" class="tab-content hidden p-4"><div id="buyer-order-list" class="space-y-3"></div></div>
        
        <div id="orders-tab" class="tab-content hidden p-4">
            <div class="grid grid-cols-2 gap-3 mb-4">
                <div class="bg-white p-3 rounded-xl border border-blue-100 shadow-sm text-center">
                    <div class="text-xs text-gray-500">ရောင်းရငွေ</div>
                    <div class="font-bold text-blue-600" id="dash-revenue">0 Ks</div>
                </div>
                <div class="bg-white p-3 rounded-xl border border-blue-100 shadow-sm text-center">
                    <div class="text-xs text-gray-500">စောင့်ဆိုင်း</div>
                    <div class="font-bold text-orange-500" id="dash-pending">0</div>
                </div>
            </div>
            <h3 class="font-bold text-gray-700 mb-3 text-sm">📦 ဖောက်သည် အော်ဒါများ</h3>
            <div id="order-list" class="space-y-3"></div>
        </div>

        <div id="toast">Message</div>

        <script>
            const tg = window.Telegram.WebApp;
            const initData = tg.initData; 
            let allProducts = [], currentCategory = 'All', cart = [];

            // 🌟 SEARCH FIX: Enter နှိပ်ပါက Keyboard ကို အလိုအလျောက် ပိတ်ပေးမည်
            function checkEnter(event) {
                if (event.key === 'Enter') {
                    searchProducts();
                    event.target.blur(); // Keyboard အောက်ဆင်းသွားစေရန်
                }
            }

            function showToast(msg) {
                const toast = document.getElementById("toast");
                toast.innerText = msg; toast.className = "show";
                setTimeout(() => { toast.className = toast.className.replace("show", ""); }, 3000);
            }

            async function apiFetch(url, options = {}) {
                const headers = { 'X-Telegram-Init-Data': initData, 'Content-Type': 'application/json', ...options.headers };
                return fetch(url, { ...options, headers });
            }

            async function initApp() {
                tg.expand(); tg.ready();
                if (!initData) return document.getElementById('display-name').innerHTML = "<span class='text-red-500'>Test Mode</span>";
                try {
                    const res = await apiFetch('/api/auth');
                    const data = await res.json();
                    document.getElementById('display-name').innerText = data.user.name;
                    if (['vendor', 'admin'].includes(data.user.role)) {
                        document.getElementById('btn-orders').classList.remove('hidden'); 
                    }
                    loadProducts();
                } catch (e) { showToast("Authentication Failed."); }
            }

            function showTab(tabId, btnId) {
                document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
                document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
                document.getElementById(tabId).classList.remove('hidden');
                if(btnId) document.getElementById(btnId).classList.add('active');
                if(tabId === 'history-tab') loadBuyerOrders();
                if(tabId === 'orders-tab') { loadVendorOrders(); loadVendorDashboard(); }
                if(tabId === 'cart-tab') renderCart();
            }

            async function loadProducts() {
                document.getElementById('loader').style.display = 'block';
                const res = await apiFetch(`/api/products?category=${currentCategory}`);
                allProducts = await res.json();
                document.getElementById('loader').style.display = 'none';
                renderProducts(allProducts);
            }

            function filterCategory(cat) {
                currentCategory = cat;
                document.querySelectorAll('.cat-chip').forEach(el => el.classList.remove('active'));
                event.target.classList.add('active');
                loadProducts();
            }

            function searchProducts() {
                const q = document.getElementById('search-box').value.toLowerCase();
                const filtered = allProducts.filter(p => p.name.toLowerCase().includes(q));
                renderProducts(filtered);
            }

            function renderProducts(products) {
                document.getElementById('product-list').innerHTML = products.map(p => {
                    const imgSrc = p.img ? `/api/image/${p.img}` : 'https://via.placeholder.com/300x200?text=Shop';
                    const isOut = p.stock <= 0;
                    return `
                    <div class="bg-white rounded-xl shadow-sm border overflow-hidden flex flex-col ${isOut ? 'opacity-60' : ''}">
                        <div class="relative">
                            <img src="${imgSrc}" class="product-img w-full h-28 object-cover border-b">
                            ${isOut ? '<span class="absolute top-2 left-2 bg-red-500 text-white text-[10px] font-bold px-2 py-1 rounded">Sold Out</span>' : `<span class="absolute top-2 left-2 bg-gray-800 text-white text-[10px] font-bold px-2 py-1 rounded">Stock: ${p.stock}</span>`}
                        </div>
                        <div class="p-3 flex-grow flex flex-col justify-between">
                            <div>
                                <div class="text-[10px] text-orange-500 font-bold mb-1 uppercase">${p.category}</div>
                                <div class="text-sm font-bold text-gray-800 line-clamp-2">${p.name}</div>
                                <div class="text-blue-600 text-sm font-black mt-1">${p.price.toLocaleString()} Ks</div>
                            </div>
                            <button onclick="addToCart(${p.id}, '${p.name.replace(/'/g, "\\'")}', ${p.price})" class="w-full mt-3 ${isOut ? 'bg-gray-200 text-gray-400' : 'bg-blue-100 text-blue-700'} text-xs py-2 rounded-lg font-bold" ${isOut ? 'disabled' : ''}>${isOut ? 'ကုန်သွားပါပြီ' : 'ခြင်းထဲထည့်ရန်'}</button>
                        </div>
                    </div>`
                }).join('');
            }

            function addToCart(id, name, price) { cart.push({id, name, price}); if(tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light'); updateCartBadge(); showToast("ခြင်းတောင်းထဲ ထည့်ပြီးပါပြီ"); }
            function updateCartBadge() { const b = document.getElementById('cart-count'); b.innerText = cart.length; cart.length > 0 ? b.classList.remove('hidden') : b.classList.add('hidden'); }
            
            function renderCart() {
                let total = 0;
                document.getElementById('cart-items').innerHTML = cart.length === 0 ? '<div class="text-center py-5 text-gray-400">ခြင်းတောင်း အလွတ်ဖြစ်နေပါသည်။</div>' : cart.map((item, index) => {
                    total += item.price;
                    return `<div class="flex justify-between bg-white p-3 rounded-lg border items-center"><span class="text-sm font-bold">${item.name}</span><div class="flex items-center gap-3"><span class="text-blue-600 font-bold text-sm">${item.price.toLocaleString()} Ks</span><button onclick="removeFromCart(${index})" class="text-red-500">✕</button></div></div>`;
                }).join('');
                document.getElementById('cart-total').innerText = `${total.toLocaleString()} Ks`;
            }

            function removeFromCart(i) { cart.splice(i, 1); updateCartBadge(); renderCart(); }

            async function checkoutCart() {
                if(cart.length === 0) return showToast("ပစ္စည်းရွေးချယ်ပါ။");
                const address = document.getElementById('checkout-address').value;
                const tx_id = document.getElementById('checkout-tx').value;
                if(!address || !tx_id) return showToast("လိပ်စာနှင့် Tx ID ထည့်ပါ။");

                tg.MainButton.showProgress();
                const res = await apiFetch(`/api/checkout`, { method: 'POST', body: JSON.stringify({ transaction_id: tx_id, address: address, cart: cart.map(i=>i.id) }) });
                tg.MainButton.hideProgress();
                
                if(res.ok) { cart = []; updateCartBadge(); document.getElementById('checkout-address').value = ''; document.getElementById('checkout-tx').value = ''; showToast("✅ အော်ဒါအောင်မြင်ပါသည်။"); showTab('history-tab', 'btn-history'); loadProducts(); } 
                else { const err = await res.json(); showToast("Error: " + err.detail); }
            }

            const statusColors = { pending: 'bg-yellow-100 text-yellow-700', approved: 'bg-blue-100 text-blue-700', shipped: 'bg-purple-100 text-purple-700', delivered: 'bg-green-100 text-green-700' };
            const statusNames = { pending: 'စစ်ဆေးဆဲ', approved: 'ငွေလက်ခံရရှိ', shipped: 'ပို့ဆောင်ပြီး', delivered: 'ရောက်ရှိပြီး' };

            async function loadBuyerOrders() {
                const res = await apiFetch('/api/buyer/orders');
                const data = await res.json();
                document.getElementById('buyer-order-list').innerHTML = data.length === 0 ? '<div class="text-center py-5 text-gray-400">အော်ဒါမရှိသေးပါ။</div>' : data.map(o => `<div class="bg-white p-3 rounded-lg shadow-sm border flex justify-between items-center"><div><div class="text-sm font-bold">${o.name}</div><div class="text-xs text-blue-600 font-bold mt-1">${o.price.toLocaleString()} Ks</div></div><span class="text-[10px] font-bold px-2 py-1 rounded-full ${statusColors[o.status]}">${statusNames[o.status]}</span></div>`).join('');
            }

            async function loadVendorDashboard() {
                const res = await apiFetch('/api/vendor/dashboard');
                const data = await res.json();
                document.getElementById('dash-revenue').innerText = `${data.total_revenue.toLocaleString()} Ks`;
                document.getElementById('dash-pending').innerText = data.pending_orders;
            }

            async function loadVendorOrders() {
                const res = await apiFetch('/api/vendor/orders');
                document.getElementById('order-list').innerHTML = (await res.json()).map(o => `
                    <div class="bg-white p-4 rounded-lg shadow-sm border">
                        <div class="flex justify-between items-start mb-2"><div class="text-sm font-bold">${o.name}</div><span class="text-[10px] font-bold px-2 py-1 rounded ${statusColors[o.status]}">${statusNames[o.status]}</span></div>
                        <div class="bg-gray-50 p-2 rounded text-xs mb-3">ဝယ်သူ: <b>${o.buyer}</b><br>လိပ်စာ: ${o.addr}<br>Tx ID: <code class="text-red-600">${o.tx}</code></div>
                        <div class="grid grid-cols-3 gap-2">
                            ${o.status === 'pending' ? `<button onclick="updateOrderStatus(${o.id}, 'approved')" class="bg-blue-500 text-white text-[10px] py-2 rounded font-bold">ငွေမှန်ကန်</button>` : ''}
                            ${['pending', 'approved'].includes(o.status) ? `<button onclick="updateOrderStatus(${o.id}, 'shipped')" class="bg-purple-500 text-white text-[10px] py-2 rounded font-bold">ပို့ဆောင်မည်</button>` : ''}
                            ${['shipped'].includes(o.status) ? `<button onclick="updateOrderStatus(${o.id}, 'delivered')" class="bg-green-500 text-white text-[10px] py-2 rounded font-bold">ရောက်ရှိပြီ</button>` : ''}
                        </div>
                    </div>`).join('');
            }

            async function updateOrderStatus(id, status) { await apiFetch(`/api/vendor/orders/${id}/status?status=${status}`, { method: 'POST' }); showToast("Status ပြောင်းလဲပြီးပါပြီ"); loadVendorOrders(); loadVendorDashboard(); }

            window.onload = initApp;
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
