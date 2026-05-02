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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://your-app-url.com")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
ADMIN_TELEGRAM_ID = os.environ.get("ADMIN_TELEGRAM_ID", "YOUR_TELEGRAM_ID") 
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "") 

bot = TeleBot(BOT_TOKEN)
app = FastAPI(title="AI-Powered Agency Mall")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_headers=["*"], allow_methods=["*"])

redis_client = None
try:
    if REDIS_URL:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
except: redis_client = None

# ==========================================
# ၂။ DATABASE MODELS
# ==========================================
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./mall_ai.db")
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
    vendor_id = Column(Integer, ForeignKey("users.id")) 
    vendor = relationship("User")

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    product_id = Column(Integer, ForeignKey("products.id"))
    transaction_id = Column(String) 
    address = Column(String) 
    status = Column(String, default="pending_approval") 
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
    cache_key = f"ai_prods_{category}_{skip}_{limit}"
    if redis_client and (cached := redis_client.get(cache_key)): return json.loads(cached)
    
    query = db.query(Product)
    if category != "All": query = query.filter(Product.category == category)
    products = query.order_by(Product.id.desc()).offset(skip).limit(limit).all()
    
    res = [{"id":p.id, "name":p.name, "price":p.price, "desc":p.description, "category":p.category, "img":p.image_file_id} for p in products]
    if redis_client: redis_client.setex(cache_key, 300, json.dumps(res))
    return res

@app.post("/api/checkout")
async def checkout_cart(req: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await req.json()
    cart_items = data.get('cart', []) 
    tx_id = data.get('transaction_id', 'Unknown')
    address = data.get('address', 'Unknown')

    if not cart_items: raise HTTPException(status_code=400)
    total_amount, ordered_names = 0, []
    vendors_to_notify = set()

    for p_id in cart_items:
        product = db.query(Product).filter(Product.id == p_id).first()
        if product:
            db.add(Order(user_id=user.id, product_id=product.id, transaction_id=tx_id, address=address))
            total_amount += product.price
            ordered_names.append(product.name)
            if product.vendor: vendors_to_notify.add((product.vendor.telegram_id, product.name))
    db.commit()

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

@app.get("/api/vendor/orders")
def get_vendor_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    orders = db.query(Order).join(Product).filter(Product.vendor_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "buyer": o.user.full_name, "tx": o.transaction_id, "addr": o.address, "status": o.status} for o in orders]

@app.post("/api/vendor/orders/{order_id}/approve")
def approve_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if order and (order.product.vendor_id == user.id or user.role == "admin"):
        order.status = "shipped"
        db.commit()
        try: bot.send_message(order.user.telegram_id, f"🚚 **{order.product.name}** ကို ပို့ဆောင်ပေးလိုက်ပါပြီ။", parse_mode="Markdown")
        except: pass
        return {"status": "success"}
    raise HTTPException(status_code=400)

# ==========================================
# ၅။ AI-POWERED TELEGRAM CMS BOT
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🏬 ကုန်တိုက်သို့ဝင်ရန်", web_app=types.WebAppInfo(WEBAPP_URL)))
    bot.send_message(message.chat.id, f"ID: `{message.from_user.id}`\n\nDigital Mall မှ ကြိုဆိုပါတယ်။", parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == str(message.from_user.id)).first()
    if not user or user.role != "admin": return
    
    msg_text = message.text.replace("/broadcast", "").strip()
    if not msg_text: return bot.reply_to(message, "စာသား ထည့်ပါ။ ဥပမာ: /broadcast ပရိုမိုးရှင်းရှိပါတယ်။")
    
    users = db.query(User).all()
    count = 0
    for u in users:
        try: 
            bot.send_message(u.telegram_id, f"📢 **အသိပေးချက်**\n\n{msg_text}", parse_mode="Markdown")
            count += 1
        except: pass
    bot.reply_to(message, f"✅ လူပေါင်း {count} ဦးထံသို့ ပေးပို့ပြီးပါပြီ။")
    db.close()

@bot.message_handler(content_types=['photo'])
def handle_cms_photo(message):
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == str(message.from_user.id)).first()
    if not user or user.role not in ["vendor", "admin"]: return db.close()

    try:
        caption = message.caption
        if not caption or "-" not in caption:
            bot.reply_to(message, "⚠️ `ပစ္စည်းအမည် - ဈေးနှုန်း` ပုံစံဖြင့် ရေးပို့ပါ။")
            return
            
        name, price = [x.strip() for x in caption.split("-")]
        price = float(price)
        file_id = message.photo[-1].file_id 

        category = "General"
        description = "အရည်အသွေးကောင်းမွန်သော ပစ္စည်းဖြစ်ပါသည်။"
        
        if GROQ_API_KEY:
            try:
                bot.reply_to(message, "⏳ AI မှ ပစ္စည်းအချက်အလက်များကို စီစဉ်နေပါသည်...")
                headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
                payload = {
                    "model": "llama3-8b-8192", 
                    "messages": [{"role": "system", "content": "You categorize products. Output JSON format: {'category': 'Electronics/Fashion/Food/General', 'description': '1 short promotional sentence in Burmese language.'}"},
                                 {"role": "user", "content": name}],
                    "response_format": {"type": "json_object"}
                }
                res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload).json()
                ai_data = json.loads(res['choices'][0]['message']['content'])
                category = ai_data.get('category', 'General')
                description = ai_data.get('description', description)
            except: pass

        new_product = Product(name=name, price=price, description=description, category=category, image_file_id=file_id, vendor_id=user.id)
        db.add(new_product)
        db.commit()
        
        if redis_client:
            for key in redis_client.scan_iter("ai_prods_*"): redis_client.delete(key)
            
        bot.reply_to(message, f"✅ ပစ္စည်းတင်ပြီးပါပြီ။\nအမည်: {name}\nအမျိုးအစား: {category}\nအညွှန်း: {description}")
    except Exception as e: bot.reply_to(message, "အမှားအယွင်း ဖြစ်ပေါ်ခဲ့ပါသည်။")
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
        <title>Agency Mall</title>
        <style>
            .tab-btn.active { color: #2563eb; border-bottom: 2px solid #2563eb; transition: 0.2s; }
            .cat-chip.active { background-color: #2563eb; color: white; border-color: #2563eb; }
            body { font-family: sans-serif; -webkit-tap-highlight-color: transparent; }
            .cart-badge { position: absolute; top: 5px; right: 10px; background: red; color: white; border-radius: 50%; padding: 2px 6px; font-size: 10px; font-weight: bold; }
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
                <input type="text" id="search-box" onkeyup="searchProducts()" placeholder="🔍 ပစ္စည်းရှာရန်..." class="w-full p-2.5 bg-white shadow-sm rounded-xl border border-gray-200 text-sm focus:ring-1 focus:ring-blue-500 outline-none mb-3">
                <div class="flex gap-2 overflow-x-auto pb-2 scrollbar-hide">
                    <button onclick="filterCategory('All')" class="cat-chip active whitespace-nowrap px-4 py-1.5 rounded-full border border-gray-300 text-xs font-bold text-gray-600">အားလုံး</button>
                    <button onclick="filterCategory('Electronics')" class="cat-chip whitespace-nowrap px-4 py-1.5 rounded-full border border-gray-300 text-xs font-bold text-gray-600">အီလက်ထရောနစ်</button>
                    <button onclick="filterCategory('Fashion')" class="cat-chip whitespace-nowrap px-4 py-1.5 rounded-full border border-gray-300 text-xs font-bold text-gray-600">ဖက်ရှင်</button>
                    <button onclick="filterCategory('Food')" class="cat-chip whitespace-nowrap px-4 py-1.5 rounded-full border border-gray-300 text-xs font-bold text-gray-600">စားသောက်ကုန်</button>
                </div>
            </div>

            <div id="vendor-panel" class="hidden p-4 mx-4 mt-2 bg-blue-50 border border-blue-200 rounded-xl">
                <p class="text-xs text-blue-700 font-bold mb-1">💡 CMS Bot System:</p>
                <p class="text-xs text-blue-600 leading-relaxed">သင့် Telegram Bot ဆီသို့ ပစ္စည်းဓာတ်ပုံ ပို့ပြီး Caption တွင် <code>ပစ္စည်းအမည် - ဈေးနှုန်း</code> ဟု ရိုက်ပို့ပါ။ AI မှ အလိုအလျောက် စီမံပေးပါမည်။</p>
            </div>
            
            <div id="loader" class="text-center mt-10 text-gray-400 text-sm">ဆွဲယူနေပါသည်...</div>
            <div id="product-list" class="p-4 grid grid-cols-2 gap-4"></div>
        </div>

        <div id="cart-tab" class="tab-content hidden p-4">
            <h2 class="font-bold mb-4 text-gray-700 text-lg">🛒 သင့်ခြင်းတောင်း</h2>
            <div id="cart-items" class="space-y-3 mb-6"></div>
            
            <div class="bg-white p-4 rounded-xl shadow-sm border border-gray-200">
                <div class="flex justify-between font-bold text-lg mb-4"><span>စုစုပေါင်း:</span> <span id="cart-total" class="text-blue-600">0 Ks</span></div>
                
                <textarea id="checkout-address" placeholder="ပို့ဆောင်ရမည့် လိပ်စာ နှင့် ဖုန်းနံပါတ်..." class="w-full p-2.5 mb-3 bg-gray-50 rounded-lg border text-sm focus:ring-1 focus:ring-blue-500 outline-none" rows="2"></textarea>
                <input type="text" id="checkout-tx" placeholder="KPay/Wave Tx ID..." class="w-full p-2.5 mb-4 bg-gray-50 rounded-lg border text-sm focus:ring-1 focus:ring-blue-500 outline-none">
                
                <button onclick="checkoutCart()" class="w-full bg-green-500 active:bg-green-600 text-white py-3 rounded-xl font-bold shadow-md">ငွေချေပြီး အော်ဒါတင်မည်</button>
            </div>
        </div>

        <div id="history-tab" class="tab-content hidden p-4"><div id="buyer-order-list" class="space-y-3"></div></div>
        <div id="orders-tab" class="tab-content hidden p-4"><div id="order-list" class="space-y-3"></div></div>

        <script>
            const tg = window.Telegram.WebApp;
            const initData = tg.initData; 
            let allProducts = [], currentCategory = 'All', cart = [];

            async function apiFetch(url, options = {}) {
                const headers = { 'X-Telegram-Init-Data': initData, 'Content-Type': 'application/json', ...options.headers };
                return fetch(url, { ...options, headers });
            }

            async function initApp() {
                tg.expand(); tg.ready();
                if (!initData) return document.getElementById('display-name').innerHTML = "<span class='text-red-500'>Error</span>";
                try {
                    const res = await apiFetch('/api/auth');
                    const data = await res.json();
                    document.getElementById('display-name').innerText = data.user.name;
                    if (['vendor', 'admin'].includes(data.user.role)) {
                        document.getElementById('vendor-panel').classList.remove('hidden');
                        document.getElementById('btn-orders').classList.remove('hidden'); 
                    }
                    loadProducts();
                } catch (e) { tg.showAlert("Authentication Failed."); }
            }

            function showTab(tabId, btnId) {
                document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
                document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
                document.getElementById(tabId).classList.remove('hidden');
                if(btnId) document.getElementById(btnId).classList.add('active');
                if(tabId === 'history-tab') loadBuyerOrders();
                if(tabId === 'orders-tab') loadVendorOrders();
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
                    return `
                    <div class="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden flex flex-col h-full">
                        <img src="${imgSrc}" class="w-full h-28 object-cover bg-gray-50 border-b border-gray-100">
                        <div class="p-3 flex-grow flex flex-col justify-between">
                            <div>
                                <div class="text-xs text-orange-500 font-bold mb-1">${p.category}</div>
                                <div class="text-sm font-bold text-gray-800 line-clamp-2">${p.name}</div>
                                <div class="text-[10px] text-gray-500 mt-1 line-clamp-2">${p.desc}</div>
                                <div class="text-blue-600 text-sm font-black mt-2">${p.price.toLocaleString()} Ks</div>
                            </div>
                            <button onclick="addToCart(${p.id}, '${p.name.replace(/'/g, "\\'")}', ${p.price})" class="w-full mt-3 bg-blue-100 text-blue-700 text-xs py-2 rounded-lg font-bold">ခြင်းထဲထည့်ရန်</button>
                        </div>
                    </div>`
                }).join('');
            }

            function addToCart(id, name, price) { cart.push({id, name, price}); tg.HapticFeedback.impactOccurred('light'); updateCartBadge(); }
            function updateCartBadge() { const b = document.getElementById('cart-count'); b.innerText = cart.length; cart.length > 0 ? b.classList.remove('hidden') : b.classList.add('hidden'); }
            
            function renderCart() {
                let total = 0;
                document.getElementById('cart-items').innerHTML = cart.length === 0 ? '<div class="text-gray-400 text-sm text-center">ခြင်းတောင်း အလွတ်ဖြစ်နေပါသည်။</div>' : cart.map((item, index) => {
                    total += item.price;
                    return `<div class="flex justify-between bg-white p-3 rounded-lg border items-center shadow-sm">
                        <span class="text-sm font-bold">${item.name}</span>
                        <div class="flex items-center gap-3"><span class="text-blue-600 font-bold text-sm">${item.price.toLocaleString()} Ks</span><button onclick="removeFromCart(${index})" class="text-red-500 font-bold">✕</button></div>
                    </div>`;
                }).join('');
                document.getElementById('cart-total').innerText = `${total.toLocaleString()} Ks`;
            }

            function removeFromCart(i) { cart.splice(i, 1); updateCartBadge(); renderCart(); }

            async function checkoutCart() {
                if(cart.length === 0) return tg.showAlert("ပစ္စည်းရွေးချယ်ပါ။");
                const address = document.getElementById('checkout-address').value;
                const tx_id = document.getElementById('checkout-tx').value;
                if(!address || !tx_id) return tg.showAlert("လိပ်စာနှင့် Tx ID ထည့်ပါ။");

                tg.MainButton.showProgress();
                const res = await apiFetch(`/api/checkout`, { method: 'POST', body: JSON.stringify({ transaction_id: tx_id, address: address, cart: cart.map(i=>i.id) }) });
                tg.MainButton.hideProgress();
                
                if(res.ok) { 
                    cart = []; updateCartBadge(); 
                    document.getElementById('checkout-address').value = ''; document.getElementById('checkout-tx').value = '';
                    tg.showAlert("✅ အော်ဒါအောင်မြင်ပါသည်။"); 
                    showTab('history-tab', 'btn-history'); 
                }
            }

            async function loadBuyerOrders() {
                const res = await apiFetch('/api/buyer/orders');
                document.getElementById('buyer-order-list').innerHTML = (await res.json()).map(o => `
                    <div class="bg-white p-3 rounded-lg shadow-sm border flex justify-between items-center">
                        <div><div class="text-sm font-bold">${o.name}</div><div class="text-xs text-gray-500">${o.price} Ks</div></div>
                        <span class="text-[10px] font-bold px-2 py-1 rounded-full ${o.status === 'shipped' ? 'bg-green-100 text-green-700' : 'bg-yellow-100 text-yellow-700'}">${o.status === 'shipped' ? 'ပို့ဆောင်ပြီး' : 'စစ်ဆေးဆဲ'}</span>
                    </div>`).join('');
            }

            async function loadVendorOrders() {
                const res = await apiFetch('/api/vendor/orders');
                document.getElementById('order-list').innerHTML = (await res.json()).map(o => `
                    <div class="bg-white p-3 rounded-lg shadow-sm border ${o.status === 'shipped' ? 'opacity-60' : ''}">
                        <div class="text-sm font-bold text-blue-700">${o.name}</div>
                        <div class="text-xs mt-1">ဝယ်သူ: <b>${o.buyer}</b> | ဖုန်း/လိပ်စာ: <span class="text-gray-700">${o.addr}</span></div>
                        <div class="text-xs mt-1">Tx ID: <code class="bg-red-50 px-1 text-red-600">${o.tx}</code></div>
                        ${o.status === 'pending_approval' ? `<button onclick="approveOrder(${o.id})" class="mt-3 w-full bg-green-500 text-white text-xs py-2 rounded shadow">အတည်ပြုမည်</button>` : `<div class="mt-3 text-center text-xs text-green-700 font-bold bg-green-50 py-2 rounded">✅ ပို့ဆောင်ပြီး</div>`}
                    </div>`).join('');
            }

            async function approveOrder(id) { if(confirm("အတည်ပြုမည်မှာ သေချာပါသလား?")) { await apiFetch(`/api/vendor/orders/${id}/approve`, { method: 'POST' }); loadVendorOrders(); } }

            window.onload = initApp;
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
