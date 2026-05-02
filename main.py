import os
import hmac
import hashlib
import json
import threading
import datetime
import time
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
# ၁။ CONFIGURATION & SETUP
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://your-render-app-url.onrender.com")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
ADMIN_TELEGRAM_ID = os.environ.get("ADMIN_TELEGRAM_ID", "YOUR_ID") 
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "") 

bot = TeleBot(BOT_TOKEN)
app = FastAPI(title="Digital Mall Auto-Run System")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_headers=["*"], allow_methods=["*"])

# Redis Cache Integration
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
    default_address = Column(String, default="") 

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
# ၄။ API ENDPOINTS (Auto-run Logic)
# ==========================================
@app.get("/api/auth")
def authenticate_user(user: User = Depends(get_current_user)):
    return {"user": {"id": user.telegram_id, "name": user.full_name, "role": user.role, "default_address": user.default_address}}

@app.get("/api/products")
def get_products(category: str = "All", search: str = "", skip: int = 0, limit: int = 15, db: Session = Depends(get_db)):
    query = db.query(Product)
    if category != "All": query = query.filter(Product.category == category)
    if search: query = query.filter(Product.name.ilike(f"%{search}%"))
    
    products = query.order_by(Product.id.desc()).offset(skip).limit(limit).all()
    res = [{"id":p.id, "name":p.name, "price":p.price, "desc":p.description, "category":p.category, "img":p.image_file_id, "stock":p.stock} for p in products]
    return res

@app.post("/api/checkout")
async def checkout_cart(req: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await req.json()
    cart_items = data.get('cart', []) 
    tx_id = data.get('transaction_id', 'Unknown')
    address = data.get('address', 'Unknown')

    if not cart_items: raise HTTPException(status_code=400, detail="Cart is empty")
    total_amount, ordered_names = 0, []
    vendors_to_notify = set()
    low_stock_alerts = []

    for item in cart_items:
        p_id = item.get('id')
        qty = item.get('qty', 1)
        product = db.query(Product).filter(Product.id == p_id).with_for_update().first()
        
        if product and product.stock >= qty:
            db.add(Order(user_id=user.id, product_id=product.id, quantity=qty, transaction_id=tx_id, address=address))
            product.stock -= qty 
            total_amount += (product.price * qty)
            ordered_names.append(f"{product.name} (x{qty})")
            if product.vendor: 
                vendors_to_notify.add((product.vendor.telegram_id, product.name, qty))
                # Auto Low-Stock Alert Logic
                if product.stock < 3:
                    low_stock_alerts.append((product.vendor.telegram_id, product.name, product.stock))
        else:
            db.rollback()
            raise HTTPException(status_code=400, detail=f"'{product.name if product else 'Item'}' ပစ္စည်းလက်ကျန်မလုံလောက်ပါ။")
            
    if address and user.default_address != address:
        user.default_address = address

    db.commit()
    if redis_client:
        for key in redis_client.scan_iter("ai_prods_render_*"): redis_client.delete(key)

    # Auto-run Notifications
    try:
        items_str = "\n".join([f"- {n}" for n in ordered_names])
        bot.send_message(user.telegram_id, f"🛒 **အော်ဒါ လက်ခံရရှိပါသည်**\n\n{items_str}\n\nစုစုပေါင်း: {total_amount} Ks\nလိပ်စာ: {address}\nTx ID: `{tx_id}`", parse_mode="Markdown")
        
        for v_tg_id, p_name, qty in vendors_to_notify:
            bot.send_message(v_tg_id, f"🔔 **အော်ဒါအသစ်ဝင်ပါသည်**\nဝယ်သူ: {user.full_name}\nပစ္စည်း: {p_name} (x{qty})\nလိပ်စာ/ဖုန်း: {address}\nTx ID: `{tx_id}`\n\nApp ထဲတွင် 'ငွေမှန်ကန်' ကြောင်း အတည်ပြုပေးပါ။", parse_mode="Markdown")
            
        for v_tg_id, p_name, stock_left in low_stock_alerts:
            bot.send_message(v_tg_id, f"⚠️ **Stock သတိပေးချက်!**\n`{p_name}` သည် လက်ကျန် ({stock_left}) ခုသာ ကျန်ပါတော့သည်။", parse_mode="Markdown")
    except: pass
    return {"status": "success"}

@app.post("/api/buyer/orders/{order_id}/cancel")
def cancel_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id, Order.user_id == user.id).with_for_update().first()
    if not order or order.status != "pending": 
        raise HTTPException(status_code=400, detail="ဤအော်ဒါကို ဖျက်၍မရတော့ပါ။")
    order.status = "cancelled"
    order.product.stock += order.quantity 
    db.commit()
    return {"status": "success"}

@app.get("/api/buyer/orders")
def get_buyer_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    orders = db.query(Order).filter(Order.user_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "qty": o.quantity, "price": o.product.price, "status": o.status} for o in orders]

# (Vendor Endpoints remain the same as previous)
@app.get("/api/vendor/dashboard")
def get_vendor_dashboard(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    orders = db.query(Order).join(Product).filter(Product.vendor_id == user.id).all()
    total_sales = sum(o.product.price * o.quantity for o in orders if o.status in ["approved", "shipped", "delivered"])
    return {"total_revenue": total_sales, "total_orders": len(orders), "pending_orders": sum(1 for o in orders if o.status == "pending")}

@app.get("/api/vendor/orders")
def get_vendor_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    orders = db.query(Order).join(Product).filter(Product.vendor_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "qty": o.quantity, "buyer": o.user.full_name, "tx": o.transaction_id, "addr": o.address, "status": o.status} for o in orders]

@app.get("/api/vendor/products")
def get_vendor_products(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    products = db.query(Product).filter(Product.vendor_id == user.id).order_by(Product.id.desc()).all()
    return [{"id":p.id, "name":p.name, "price":p.price, "stock":p.stock, "category":p.category} for p in products]

@app.delete("/api/vendor/products/{product_id}")
def delete_product(product_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user.id).first()
    if not product: raise HTTPException(status_code=404)
    db.delete(product)
    db.commit()
    return {"status": "success"}

@app.post("/api/vendor/orders/{order_id}/status")
def update_order_status(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    status_map = {"approved": "✅ ငွေလွှဲမှန်ကန်ပါသည်။", "shipped": "🚚 ပစ္စည်းပို့ဆောင်ပေးလိုက်ပါပြီ။", "delivered": "🎁 ပစ္စည်းလက်ခံရရှိကြောင်း မှတ်တမ်းတင်ပြီးပါပြီ။", "cancelled": "❌ အော်ဒါအား ပယ်ဖျက်လိုက်ပါသည်။"}
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or (order.product.vendor_id != user.id and user.role != "admin"): raise HTTPException(status_code=400)
    
    new_status = request.query_params.get("status", "shipped")
    if new_status == "cancelled" and order.status != "cancelled": order.product.stock += order.quantity 
    order.status = new_status
    db.commit()
    try: 
        if new_status in status_map: bot.send_message(order.user.telegram_id, f"{status_map[new_status]}\nပစ္စည်း: **{order.product.name} (x{order.quantity})**", parse_mode="Markdown")
    except: pass
    return {"status": "success"}

# ==========================================
# ၅။ AI-POWERED CMS & CHAT BOT
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🏬 ကုန်တိုက်သို့ဝင်ရန်", web_app=types.WebAppInfo(WEBAPP_URL)))
    bot.send_message(message.chat.id, f"မင်္ဂလာပါရှင်။ ဖိနပ်၊ အင်္ကျီ စသည်ဖြင့် ရှာဖွေချင်သည့် ပစ္စည်းကို စာရိုက်ပြီး မေးမြန်းနိုင်သလို၊ အောက်ပါခလုတ်ကို နှိပ်၍လည်း ဈေးဝယ်နိုင်ပါသည်။", reply_markup=markup)

@bot.message_handler(content_types=['text'])
def handle_text_search(message):
    user_text = message.text
    if not GROQ_API_KEY: return
    
    try:
        # AI Natural Language Search Intent parsing
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        prompt = f"""
        Analyze the Burmese text: "{user_text}". 
        Is the user asking to buy or search for a product? 
        If yes, extract the main search term in Burmese (e.g. "ဖိနပ်").
        Return strictly in JSON: {{"intent": "buy" or "other", "search_term": "extracted_term"}}
        """
        payload = {"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}
        res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload).json()
        parsed = json.loads(res['choices'][0]['message']['content'])
        
        if parsed.get("intent") == "buy" and parsed.get("search_term"):
            search_url = f"{WEBAPP_URL}?search={parsed['search_term']}"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(f"🔍 '{parsed['search_term']}' ကို ရှာရန်နှိပ်ပါ", web_app=types.WebAppInfo(search_url)))
            bot.reply_to(message, f"'{parsed['search_term']}' နဲ့ ပတ်သက်တဲ့ ပစ္စည်းတွေကို အောက်ကခလုတ်မှာ နှိပ်ကြည့်လိုက်ပါ။", reply_markup=markup)
    except: pass

@bot.message_handler(content_types=['photo'])
def handle_cms_photo(message):
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == str(message.from_user.id)).first()
    if not user or user.role not in ["vendor", "admin"]: return db.close()

    try:
        caption = message.caption or "New Product"
        file_id = message.photo[-1].file_id 
        ai_data = {"name": "New Product", "price": 0, "category": "General", "description": caption, "stock": 10}
        
        if GROQ_API_KEY:
            try:
                msg = bot.reply_to(message, "⏳ AI စနစ်ဖြင့် အလိုအလျောက် စာရင်းသွင်းနေပါသည်...")
                headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
                prompt = f"""
                Analyze the following Burmese text for an e-commerce product: "{caption}"
                Extract the details and return strictly in JSON format.
                Required keys: 'name', 'price' (numeric, default 0), 'category' (Electronics, Fashion, Food, General), 'description', 'stock' (numeric, default 10).
                """
                payload = {"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}
                res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload).json()
                parsed = json.loads(res['choices'][0]['message']['content'])
                
                for k in ['name', 'price', 'category', 'description', 'stock']:
                    if parsed.get(k): ai_data[k] = parsed[k]
                
                bot.delete_message(message.chat.id, msg.message_id)
            except: pass

        new_product = Product(name=ai_data['name'], price=float(ai_data['price']), description=ai_data['description'], category=ai_data['category'], stock=int(ai_data['stock']), image_file_id=file_id, vendor_id=user.id)
        db.add(new_product)
        db.commit()
        bot.reply_to(message, f"✅ ပစ္စည်း အလိုအလျောက် တင်ပြီးပါပြီ။\n\n📌 အမည်: {ai_data['name']}\n💰 ဈေးနှုန်း: {ai_data['price']} Ks")
    except Exception as e: bot.reply_to(message, f"အမှားအယွင်း ဖြစ်ပေါ်ခဲ့ပါသည်။ ({str(e)})")
    finally: db.close()

# ==========================================
# ၆။ FRONTEND UI (No-button, Auto-run focus)
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
        <title>Digital Mall Auto</title>
        <style>
            .tab-btn.active { color: #2563eb; border-bottom: 2px solid #2563eb; transition: 0.2s; }
            .cat-chip.active { background-color: #2563eb; color: white; border-color: #2563eb; }
            body { font-family: sans-serif; -webkit-tap-highlight-color: transparent; overscroll-behavior-y: none; }
            .cart-badge { position: absolute; top: 5px; right: 10px; background: #ef4444; color: white; border-radius: 50%; padding: 2px 6px; font-size: 10px; font-weight: bold; }
            img.product-img { -webkit-touch-callout: none; -webkit-user-select: none; pointer-events: none; }
            #toast { visibility: hidden; min-width: 250px; background-color: #333; color: #fff; text-align: center; border-radius: 8px; padding: 12px; position: fixed; z-index: 60; left: 50%; bottom: 30px; transform: translateX(-50%); font-size: 14px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
            #toast.show { visibility: visible; animation: fadein 0.3s, fadeout 0.3s 2.5s; }
            @keyframes fadein { from {bottom: 0; opacity: 0;} to {bottom: 30px; opacity: 1;} }
            @keyframes fadeout { from {bottom: 30px; opacity: 1;} to {bottom: 0; opacity: 0;} }
            
            /* Quick Buy Modal */
            #quick-buy-modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 50; align-items: flex-end; justify-content: center; }
            #quick-buy-modal.active { display: flex; animation: slideup 0.3s ease-out; }
            @keyframes slideup { from { transform: translateY(100%); } to { transform: translateY(0); } }
        </style>
    </head>
    <body class="bg-gray-50 min-h-screen pb-24">
        
        <header class="bg-white p-4 shadow-sm sticky top-0 z-40 flex justify-between items-center">
            <span class="font-bold text-blue-600 text-lg">Digital Mall</span>
            <div class="flex gap-2">
                <button onclick="showTab('cart-tab', 'btn-shop')" class="relative bg-gray-100 p-2 rounded-full active:bg-gray-200">
                    🛒 <span id="cart-count" class="cart-badge hidden">0</span>
                </button>
                <div class="text-xs bg-blue-50 px-3 py-2 rounded-full text-blue-700 font-semibold truncate max-w-[100px]" id="display-name">Loading...</div>
            </div>
        </header>

        <div id="nav-bar" class="flex justify-around bg-white border-b text-sm font-bold text-gray-400">
            <button id="btn-shop" onclick="showTab('shop-tab', 'btn-shop')" class="tab-btn active w-full py-3 transition-colors">ဈေးဝယ်ရန်</button>
            <button id="btn-history" onclick="showTab('history-tab', 'btn-history')" class="tab-btn w-full py-3 transition-colors">မှတ်တမ်း</button>
            <button id="btn-orders" onclick="showTab('orders-tab', 'btn-orders')" class="tab-btn hidden w-full py-3 transition-colors">စီမံရန်</button>
        </div>

        <div id="shop-tab" class="tab-content">
            <div class="px-4 pt-4">
                <input type="text" id="search-box" oninput="autoSearch()" placeholder="🔍 ပစ္စည်းအမည် ရိုက်ထည့်ပါ..." class="w-full p-3 bg-white shadow-sm rounded-xl border border-gray-200 text-sm focus:ring-2 focus:ring-blue-500 outline-none mb-3 transition-shadow">
                <div class="flex gap-2 overflow-x-auto pb-2 scrollbar-hide">
                    <button onclick="filterCategory('All')" class="cat-chip active whitespace-nowrap px-4 py-1.5 rounded-full border border-gray-300 text-xs font-bold text-gray-600 transition-colors">အားလုံး</button>
                    <button onclick="filterCategory('Electronics')" class="cat-chip whitespace-nowrap px-4 py-1.5 rounded-full border border-gray-300 text-xs font-bold text-gray-600 transition-colors">အီလက်ထရောနစ်</button>
                    <button onclick="filterCategory('Fashion')" class="cat-chip whitespace-nowrap px-4 py-1.5 rounded-full border border-gray-300 text-xs font-bold text-gray-600 transition-colors">ဖက်ရှင်</button>
                    <button onclick="filterCategory('Food')" class="cat-chip whitespace-nowrap px-4 py-1.5 rounded-full border border-gray-300 text-xs font-bold text-gray-600 transition-colors">စားသောက်ကုန်</button>
                </div>
            </div>
            <div id="product-list" class="p-4 grid grid-cols-2 gap-4"></div>
        </div>

        <div id="cart-tab" class="tab-content hidden p-4">
            <h2 class="font-bold mb-4 text-gray-700 text-lg flex justify-between">🛒 သင့်ခြင်းတောင်း <button onclick="clearCart()" class="text-sm text-red-500 font-normal bg-red-50 px-3 py-1 rounded-full active:bg-red-100">အကုန်ဖျက်မည်</button></h2>
            <div id="cart-items" class="space-y-3 mb-6"></div>
            <div class="bg-white p-5 rounded-2xl shadow-sm border border-gray-200">
                <div class="flex justify-between font-bold text-lg mb-4 border-b pb-3"><span>စုစုပေါင်း:</span> <span id="cart-total" class="text-blue-600">0 Ks</span></div>
                <textarea id="checkout-address" placeholder="ပို့ဆောင်ရမည့် လိပ်စာ နှင့် ဖုန်းနံပါတ်..." class="w-full p-3 mb-3 bg-gray-50 rounded-xl border text-sm focus:ring-2 focus:ring-blue-500 outline-none" rows="2"></textarea>
                <input type="text" id="checkout-tx" placeholder="KPay/Wave Tx ID..." class="w-full p-3 mb-4 bg-gray-50 rounded-xl border text-sm focus:ring-2 focus:ring-blue-500 outline-none">
                <button onclick="checkoutCart(cart)" class="w-full bg-blue-600 hover:bg-blue-700 active:bg-blue-800 text-white py-3.5 rounded-xl font-bold transition-colors">အော်ဒါတင်မည်</button>
            </div>
        </div>

        <div id="quick-buy-modal" onclick="if(event.target===this) closeQuickBuy()">
            <div class="bg-white w-full rounded-t-3xl p-5 shadow-2xl">
                <div class="flex justify-between items-center mb-4 border-b pb-3">
                    <h3 class="font-bold text-lg">ချက်ချင်းဝယ်မည်</h3>
                    <button onclick="closeQuickBuy()" class="text-gray-400 bg-gray-100 rounded-full w-8 h-8 font-bold">X</button>
                </div>
                <div id="qb-item-details" class="mb-4 font-bold text-blue-600"></div>
                <textarea id="qb-address" placeholder="ပို့ဆောင်ရမည့် လိပ်စာ နှင့် ဖုန်းနံပါတ်..." class="w-full p-3 mb-3 bg-gray-50 rounded-xl border text-sm focus:ring-2 focus:ring-blue-500 outline-none" rows="2"></textarea>
                <input type="text" id="qb-tx" placeholder="KPay/Wave Tx ID..." class="w-full p-3 mb-4 bg-gray-50 rounded-xl border text-sm focus:ring-2 focus:ring-blue-500 outline-none">
                <button id="qb-btn" class="w-full bg-orange-500 active:bg-orange-600 text-white py-3.5 rounded-xl font-bold shadow-md transition-colors">ယခုဝယ်မည်</button>
            </div>
        </div>

        <div id="history-tab" class="tab-content hidden p-4"><div id="buyer-order-list" class="space-y-3"></div></div>
        
        <div id="orders-tab" class="tab-content hidden p-4">
            <div class="flex gap-2 mb-4 bg-gray-100 p-1 rounded-xl">
                <button onclick="switchVendorTab('dash')" id="v-tab-dash" class="flex-1 bg-white shadow-sm py-2.5 rounded-lg text-sm font-bold text-blue-600">အော်ဒါများ</button>
                <button onclick="switchVendorTab('prods')" id="v-tab-prods" class="flex-1 py-2.5 rounded-lg text-sm font-bold text-gray-500">ပစ္စည်းများ</button>
            </div>
            <div id="vendor-dash-view"><div id="order-list" class="space-y-3"></div></div>
            <div id="vendor-prods-view" class="hidden"><div id="vendor-product-list" class="space-y-3"></div></div>
        </div>

        <div id="toast">Message</div>

        <script>
            const tg = window.Telegram.WebApp;
            const initData = tg.initData; 
            let allProducts = [], currentCategory = 'All', cart = [];
            let userDefaultAddress = ""; 
            let searchTimeout = null;
            let quickBuyItem = null;

            function showToast(msg) {
                const t = document.getElementById("toast");
                t.innerText = msg; t.className = "show";
                if(tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
                setTimeout(() => { t.className = t.className.replace("show", ""); }, 2800);
            }

            async function apiFetch(url, options = {}) {
                return fetch(url, { ...options, headers: { 'X-Telegram-Init-Data': initData, 'Content-Type': 'application/json', ...options.headers }});
            }

            async function initApp() {
                tg.expand(); tg.ready();
                
                // Read URL Search Params (From Bot Deep Link)
                const urlParams = new URLSearchParams(window.location.search);
                const botSearch = urlParams.get('search');
                
                try {
                    const res = await apiFetch('/api/auth');
                    const data = await res.json();
                    document.getElementById('display-name').innerText = data.user.name;
                    userDefaultAddress = data.user.default_address || "";
                    document.getElementById('checkout-address').value = userDefaultAddress;
                    document.getElementById('qb-address').value = userDefaultAddress;

                    if (['vendor', 'admin'].includes(data.user.role)) document.getElementById('btn-orders').classList.remove('hidden');
                    
                    if (botSearch) {
                        document.getElementById('search-box').value = botSearch;
                        autoSearch();
                    } else {
                        loadProducts();
                    }
                } catch (e) { showToast("Authentication Failed."); }
            }

            function showTab(tabId, btnId) {
                if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
                document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
                document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
                document.getElementById(tabId).classList.remove('hidden');
                if(btnId) document.getElementById(btnId).classList.add('active');
                
                if(tabId === 'history-tab') loadBuyerOrders();
                if(tabId === 'orders-tab') switchVendorTab('dash');
                if(tabId === 'cart-tab') renderCart();
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }

            async function loadProducts(query = "") {
                const res = await apiFetch(`/api/products?category=${currentCategory}&search=${query}`);
                allProducts = await res.json();
                renderProducts(allProducts);
            }

            function filterCategory(cat) {
                if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
                currentCategory = cat; document.getElementById('search-box').value = "";
                document.querySelectorAll('.cat-chip').forEach(el => el.classList.remove('active'));
                event.target.classList.add('active');
                loadProducts();
            }

            function autoSearch() {
                clearTimeout(searchTimeout);
                searchTimeout = setTimeout(() => { loadProducts(document.getElementById('search-box').value); }, 300);
            }

            function renderProducts(products) {
                document.getElementById('product-list').innerHTML = products.map(p => {
                    const imgSrc = p.img ? `/api/image/${p.img}` : 'https://via.placeholder.com/300';
                    const isOut = p.stock <= 0;
                    return `
                    <div class="bg-white rounded-2xl shadow-sm border border-gray-100 overflow-hidden flex flex-col ${isOut ? 'opacity-50 grayscale' : ''}">
                        <div class="relative"><img src="${imgSrc}" class="product-img w-full h-32 object-cover border-b"></div>
                        <div class="p-3 flex-grow flex flex-col justify-between">
                            <div>
                                <div class="text-sm font-bold text-gray-800 line-clamp-2">${p.name}</div>
                                <div class="text-blue-600 text-sm font-black mt-1">${p.price.toLocaleString()} Ks</div>
                            </div>
                            <div class="flex gap-1 mt-3">
                                <button onclick="addToCart(${p.id}, '${p.name.replace(/'/g, "\\'")}', ${p.price}, ${p.stock})" class="flex-1 bg-blue-50 text-blue-700 text-xs py-2 rounded-lg font-bold" ${isOut?'disabled':''}>🛒 ထည့်ရန်</button>
                                <button onclick="openQuickBuy(${p.id}, '${p.name.replace(/'/g, "\\'")}', ${p.price}, ${p.stock})" class="flex-1 bg-orange-500 text-white text-xs py-2 rounded-lg font-bold shadow-sm" ${isOut?'disabled':''}>⚡ ဝယ်မည်</button>
                            </div>
                        </div>
                    </div>`
                }).join('');
            }

            function addToCart(id, name, price, maxStock) { 
                let existing = cart.find(i => i.id === id);
                if(existing) { if (existing.qty < maxStock) existing.qty += 1; else return showToast("လက်ကျန် မလုံလောက်ပါ။"); } 
                else cart.push({id, name, price, qty: 1, maxStock});
                if(tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light'); 
                updateCartBadge(); showToast("ခြင်းထဲရောက်ပါပြီ"); 
            }
            
            function openQuickBuy(id, name, price, maxStock) {
                if(tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium');
                quickBuyItem = {id, name, price, qty: 1, maxStock};
                document.getElementById('qb-item-details').innerText = `${name} - ${price.toLocaleString()} Ks`;
                document.getElementById('quick-buy-modal').classList.add('active');
                document.getElementById('qb-btn').onclick = () => checkoutCart([quickBuyItem], 'qb-tx', 'qb-address');
            }
            function closeQuickBuy() { document.getElementById('quick-buy-modal').classList.remove('active'); }

            function updateCartBadge() { 
                const b = document.getElementById('cart-count'); 
                let t = cart.reduce((s, i) => s + i.qty, 0); b.innerText = t; 
                t > 0 ? b.classList.remove('hidden') : b.classList.add('hidden'); 
            }
            
            function renderCart() {
                let total = 0;
                document.getElementById('cart-items').innerHTML = cart.map((i) => {
                    total += (i.price * i.qty);
                    return `<div class="flex justify-between items-center bg-white p-3 rounded-2xl border shadow-sm mb-2"><span class="text-sm font-bold w-2/3 line-clamp-1">${i.name} (x${i.qty})</span><span class="text-blue-600 font-bold">${(i.price * i.qty).toLocaleString()}</span></div>`;
                }).join('');
                document.getElementById('cart-total').innerText = `${total.toLocaleString()} Ks`;
            }

            async function checkoutCart(itemsArray, txId = 'checkout-tx', addrId = 'checkout-address') {
                if(itemsArray.length === 0) return;
                const address = document.getElementById(addrId).value;
                const tx_id = document.getElementById(txId).value;
                if(!address || !tx_id) return showToast("လိပ်စာနှင့် Tx ID ထည့်ပါ။");

                tg.MainButton.showProgress();
                const res = await apiFetch(`/api/checkout`, { method: 'POST', body: JSON.stringify({ transaction_id: tx_id, address: address, cart: itemsArray.map(i=>({id:i.id, qty:i.qty})) }) });
                tg.MainButton.hideProgress();
                
                if(res.ok) { 
                    if(itemsArray === cart) { clearCart(); document.getElementById(txId).value = ''; }
                    else { closeQuickBuy(); document.getElementById('qb-tx').value = ''; }
                    showToast("✅ အော်ဒါအောင်မြင်ပါသည်။"); 
                    showTab('history-tab', 'btn-history'); loadProducts(); 
                } else { showToast("Error"); } 
            }

            function clearCart() { cart = []; updateCartBadge(); renderCart(); }

            // Buyer and Vendor Logic continues same as before...
            async function loadBuyerOrders() {
                const res = await apiFetch('/api/buyer/orders');
                document.getElementById('buyer-order-list').innerHTML = (await res.json()).map(o => `
                    <div class="bg-white p-4 rounded-2xl shadow-sm border border-gray-100">
                        <div class="flex justify-between"><span class="text-sm font-bold">${o.name} (x${o.qty})</span><span class="text-[10px] font-bold px-2.5 py-1 rounded-md border">${o.status}</span></div>
                    </div>`).join('');
            }
            
            function switchVendorTab(tab) {
                document.getElementById('vendor-dash-view').style.display = tab === 'dash' ? 'block' : 'none';
                document.getElementById('vendor-prods-view').style.display = tab === 'prods' ? 'block' : 'none';
                if(tab === 'dash') loadVendorOrders(); else loadVendorProducts();
            }

            async function loadVendorOrders() {
                const res = await apiFetch('/api/vendor/orders');
                document.getElementById('order-list').innerHTML = (await res.json()).map(o => `
                    <div class="bg-white p-4 rounded-2xl shadow-sm border mb-2">
                        <div class="text-sm font-bold mb-2">${o.name} <span class="text-blue-500">(x${o.qty})</span></div>
                        <div class="text-xs text-gray-600 mb-2">ဝယ်သူ: ${o.buyer}<br>လိပ်စာ: ${o.addr}<br>Tx ID: ${o.tx}</div>
                        <button onclick="apiFetch('/api/vendor/orders/${o.id}/status?status=approved', {method:'POST'}).then(loadVendorOrders)" class="bg-blue-50 text-blue-700 px-3 py-1.5 rounded-lg text-xs font-bold">ငွေမှန်ကန်</button>
                    </div>`).join('');
            }

            async function loadVendorProducts() {
                const res = await apiFetch('/api/vendor/products');
                document.getElementById('vendor-product-list').innerHTML = (await res.json()).map(p => `
                    <div class="bg-white p-4 rounded-2xl shadow-sm border flex justify-between items-center mb-2">
                        <div><div class="text-sm font-bold">${p.name}</div><div class="text-[11px] text-gray-500">Stock: ${p.stock}</div></div>
                        <button onclick="apiFetch('/api/vendor/products/${p.id}', {method:'DELETE'}).then(loadVendorProducts)" class="text-red-500 text-xs font-bold bg-red-50 px-3 py-2 rounded-xl">ဖျက်မည်</button>
                    </div>`).join('');
            }

            window.onload = initApp;
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
