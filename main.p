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

# Payment Info (Admin/Platform)
PAYMENT_INFO = {
    "kpay": "09123456789 (U Ba)",
    "wave": "09123456789 (U Ba)"
}

bot = TeleBot(BOT_TOKEN)
app = FastAPI(title="Digital Mall Auto-Run System Pro")
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
    default_address = Column(String, default="") 
    phone = Column(String, default="")

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
    status = Column(String, default="pending") # pending, approved, shipped, delivered, cancelled
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
# ၄။ API ENDPOINTS (Core Business Logic)
# ==========================================
@app.get("/api/auth")
def authenticate_user(user: User = Depends(get_current_user)):
    return {
        "user": {
            "id": user.telegram_id, 
            "name": user.full_name, 
            "role": user.role, 
            "default_address": user.default_address,
            "phone": user.phone
        }, 
        "payment_info": PAYMENT_INFO
    }

@app.get("/api/products")
def get_products(category: str = "All", search: str = "", skip: int = 0, limit: int = 20, db: Session = Depends(get_db)):
    query = db.query(Product)
    if category != "All": query = query.filter(Product.category == category)
    if search: query = query.filter(Product.name.ilike(f"%{search}%"))
    
    products = query.order_by(Product.id.desc()).offset(skip).limit(limit).all()
    categories = [c[0] for c in db.query(Product.category).distinct().all()] 
    res = [{"id":p.id, "name":p.name, "price":p.price, "desc":p.description, "category":p.category, "img":p.image_file_id, "stock":p.stock} for p in products]
    return {"products": res, "categories": categories}

@app.post("/api/checkout")
async def checkout_cart(req: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await req.json()
    cart_items = data.get('cart', []) 
    tx_id = data.get('transaction_id', 'Unknown')
    address = data.get('address', 'Unknown')
    phone = data.get('phone', '')

    if not cart_items: raise HTTPException(status_code=400, detail="Cart is empty")
    total_amount, ordered_names = 0, []
    vendors_to_notify = set()

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
        else:
            db.rollback()
            raise HTTPException(status_code=400, detail=f"'{product.name if product else 'Item'}' ပစ္စည်းလက်ကျန်မလုံလောက်ပါ။")
            
    if address and user.default_address != address:
        user.default_address = address
    if phone and user.phone != phone:
        user.phone = phone

    db.commit()

    # Auto-run Notifications
    try:
        items_str = "\n".join([f"- {n}" for n in ordered_names])
        bot.send_message(user.telegram_id, f"🛒 **အော်ဒါ လက်ခံရရှိပါသည်**\n\n{items_str}\n\nစုစုပေါင်း: {total_amount:,.0f} Ks\nပို့ဆောင်ရမည့်လိပ်စာ: {address}\nTx ID: `{tx_id}`\n\n_ရောင်းချသူမှ ငွေသွင်းမှတ်တမ်း စစ်ဆေးပြီးပါက ဆက်လက်အကြောင်းကြားပေးပါမည်။_", parse_mode="Markdown")
        
        for v_tg_id, p_name, qty in vendors_to_notify:
            bot.send_message(v_tg_id, f"🔔 **အော်ဒါအသစ်ဝင်ပါသည်**\nဝယ်သူ: {user.full_name}\nပစ္စည်း: {p_name} (x{qty})\nလိပ်စာ: {address}\nTx ID: `{tx_id}`\n\nApp ထဲတွင် အော်ဒါအခြေအနေကို အတည်ပြုပေးပါ။", parse_mode="Markdown")
    except: pass
    return {"status": "success"}

@app.get("/api/buyer/orders")
def get_buyer_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    orders = db.query(Order).filter(Order.user_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "qty": o.quantity, "price": o.product.price, "status": o.status, "date": o.created_at.strftime("%Y-%m-%d")} for o in orders]

@app.post("/api/buyer/orders/{order_id}/cancel")
def cancel_buyer_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id, Order.user_id == user.id).first()
    if not order: raise HTTPException(status_code=404, detail="Order not found")
    if order.status != "pending": raise HTTPException(status_code=400, detail="Only pending orders can be cancelled")
    
    order.status = "cancelled"
    order.product.stock += order.quantity 
    db.commit()
    
    if order.product.vendor:
        try: bot.send_message(order.product.vendor.telegram_id, f"⚠️ ဝယ်သူ {user.full_name} မှ အော်ဒါဖျက်သိမ်းလိုက်ပါသည်။\nပစ္စည်း: {order.product.name} (x{order.quantity})")
        except: pass
    return {"status": "success"}

# Vendor Endpoints
@app.get("/api/vendor/orders")
def get_vendor_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    orders = db.query(Order).join(Product).filter(Product.vendor_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "qty": o.quantity, "buyer": o.user.full_name, "tx": o.transaction_id, "addr": o.address, "status": o.status} for o in orders]

@app.post("/api/vendor/orders/{order_id}/status")
def update_order_status(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    status_map = {
        "approved": "✅ ငွေလွှဲမှန်ကန်ပါသည်။ ထုပ်ပိုးနေပါသည်။", 
        "shipped": "🚚 ပစ္စည်းပို့ဆောင်ပေးလိုက်ပါပြီ။", 
        "delivered": "🎁 ပစ္စည်းလက်ခံရရှိကြောင်း မှတ်တမ်းတင်ပြီးပါပြီ။", 
        "cancelled": "❌ အော်ဒါအား ပယ်ဖျက်လိုက်ပါသည်။"
    }
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or (order.product.vendor_id != user.id and user.role != "admin"): raise HTTPException(status_code=400)
    
    new_status = request.query_params.get("status")
    if new_status not in status_map: raise HTTPException(status_code=400)

    if new_status == "cancelled" and order.status != "cancelled": 
        order.product.stock += order.quantity 
    order.status = new_status
    db.commit()
    try: 
        bot.send_message(order.user.telegram_id, f"{status_map[new_status]}\nပစ္စည်း: **{order.product.name} (x{order.quantity})**", parse_mode="Markdown")
    except: pass
    return {"status": "success"}

@app.get("/api/vendor/products")
def get_vendor_products(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    products = db.query(Product).filter(Product.vendor_id == user.id).order_by(Product.id.desc()).all()
    return [{"id":p.id, "name":p.name, "price":p.price, "stock":p.stock} for p in products]

@app.put("/api/vendor/products/{product_id}/stock")
async def update_product_stock(product_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    data = await request.json()
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user.id).first()
    if not product: raise HTTPException(status_code=404)
    product.stock = data.get("stock", product.stock)
    db.commit()
    return {"status": "success"}

@app.put("/api/vendor/products/{product_id}/edit")
async def edit_product_info(product_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    data = await request.json()
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user.id).first()
    if not product: raise HTTPException(status_code=404)
    if "name" in data: product.name = data["name"]
    if "price" in data: product.price = float(data["price"])
    db.commit()
    return {"status": "success"}

@app.delete("/api/vendor/products/{product_id}")
def delete_product(product_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user.id).first()
    if not product: raise HTTPException(status_code=404)
    db.delete(product)
    db.commit()
    return {"status": "success"}

# ==========================================
# ၅။ AI-POWERED CMS & CHAT BOT
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🏬 ကုန်တိုက်သို့ဝင်ရန်", web_app=types.WebAppInfo(WEBAPP_URL)))
    bot.send_message(message.chat.id, f"မင်္ဂလာပါရှင်။ ဖိနပ်၊ အင်္ကျီ စသည်ဖြင့် ရှာဖွေချင်သည့် ပစ္စည်းကို စာရိုက်ပြီး မေးမြန်းနိုင်သလို၊ အောက်ပါခလုတ်ကို နှိပ်၍လည်း ဈေးဝယ်နိုင်ပါသည်။", reply_markup=markup)

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
                msg = bot.reply_to(message, "⏳ AI ဖြင့် ပစ္စည်းအချက်အလက်များကို ခွဲခြမ်းစိတ်ဖြာနေပါသည်...")
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
        bot.reply_to(message, f"✅ **ပစ္စည်း အလိုအလျောက် တင်ပြီးပါပြီ။**\n\n📌 အမည်: {ai_data['name']}\n💰 ဈေးနှုန်း: {ai_data['price']} Ks\n📦 အရေအတွက်: {ai_data['stock']}\n\n_App ထဲသို့ဝင်၍ အလွယ်တကူ ထပ်မံပြင်ဆင်နိုင်ပါသည်။_", parse_mode="Markdown")
    except Exception as e: bot.reply_to(message, f"အမှားအယွင်း ဖြစ်ပေါ်ခဲ့ပါသည်။")
    finally: db.close()

# ==========================================
# ၆။ FRONTEND UI (User-Friendly UX Focus)
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
            body { font-family: sans-serif; -webkit-tap-highlight-color: transparent; background-color: #f3f4f6; }
            .tab-btn.active { color: #2563eb; border-bottom: 3px solid #2563eb; }
            .cat-chip.active { background-color: #2563eb; color: white; border-color: #2563eb; }
            .cart-badge { position: absolute; top: -2px; right: -2px; background: #ef4444; color: white; border-radius: 50%; padding: 2px 6px; font-size: 10px; font-weight: bold; box-shadow: 0 2px 4px rgba(0,0,0,0.2);}
            
            #toast { visibility: hidden; min-width: 250px; background-color: rgba(31, 41, 55, 0.95); color: #fff; text-align: center; border-radius: 12px; padding: 14px; position: fixed; z-index: 100; left: 50%; bottom: 80px; transform: translateX(-50%); font-size: 14px; backdrop-filter: blur(4px); box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1); }
            #toast.show { visibility: visible; animation: fadein 0.3s, fadeout 0.3s 2.5s; }
            @keyframes fadein { from {bottom: 50px; opacity: 0;} to {bottom: 80px; opacity: 1;} }
            @keyframes fadeout { from {bottom: 80px; opacity: 1;} to {bottom: 50px; opacity: 0;} }
            
            .status-pending { background-color: #fef3c7; color: #d97706; }
            .status-approved { background-color: #dbeafe; color: #2563eb; }
            .status-shipped { background-color: #f3e8ff; color: #9333ea; }
            .status-delivered { background-color: #dcfce3; color: #166534; }
            .status-cancelled { background-color: #fee2e2; color: #dc2626; }

            .empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 40px 20px; text-align: center; color: #6b7280; }
            .empty-icon { font-size: 48px; margin-bottom: 16px; opacity: 0.5; }
        </style>
    </head>
    <body class="pb-20">
        
        <header class="bg-white p-4 shadow-sm sticky top-0 z-40 flex justify-between items-center">
            <span class="font-bold text-blue-700 text-xl tracking-tight">Digital<span class="text-gray-800">Mall</span></span>
            <div class="flex items-center gap-3">
                <div class="text-xs bg-gray-100 px-3 py-1.5 rounded-full text-gray-700 font-medium max-w-[120px] truncate border border-gray-200" id="display-name">...</div>
                <button onclick="showTab('cart-tab', 'btn-shop')" class="relative p-2.5 rounded-full bg-blue-50 text-blue-600 active:bg-blue-100 transition">
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 3h2l.4 2M7 13h10l4-8H5.4M7 13L5.4 5M7 13l-2.293 2.293c-.63.63-.184 1.707.707 1.707H17m0 0a2 2 0 100 4 2 2 0 000-4zm-8 2a2 2 0 11-4 0 2 2 0 014 0z" /></svg>
                    <span id="cart-count" class="cart-badge hidden">0</span>
                </button>
            </div>
        </header>

        <div class="fixed bottom-0 w-full bg-white border-t flex justify-around text-xs font-medium text-gray-500 z-50 pb-safe shadow-[0_-5px_10px_rgba(0,0,0,0.05)]">
            <button id="btn-shop" onclick="showTab('shop-tab', 'btn-shop')" class="tab-btn active flex-1 py-4 flex flex-col items-center gap-1 transition-colors">
                <span class="text-lg">🏠</span><span>ဈေးဝယ်မည်</span>
            </button>
            <button id="btn-history" onclick="showTab('history-tab', 'btn-history')" class="tab-btn flex-1 py-4 flex flex-col items-center gap-1 transition-colors">
                <span class="text-lg">📋</span><span>မှတ်တမ်း</span>
            </button>
            <button id="btn-orders" onclick="showTab('orders-tab', 'btn-orders')" class="tab-btn hidden flex-1 py-4 flex flex-col items-center gap-1 transition-colors">
                <span class="text-lg">⚙️</span><span>စီမံရန်</span>
            </button>
        </div>

        <div id="shop-tab" class="tab-content">
            <div class="p-4 bg-white shadow-sm mb-2 rounded-b-2xl">
                <div class="relative">
                    <span class="absolute inset-y-0 left-0 flex items-center pl-3 text-gray-400">🔍</span>
                    <input type="text" id="search-box" oninput="autoSearch()" placeholder="ရှာဖွေလိုသော ပစ္စည်းအမည်..." class="w-full pl-10 pr-3 py-3 bg-gray-50 rounded-xl border border-gray-200 text-sm focus:ring-2 focus:ring-blue-500 outline-none mb-4 transition-all">
                </div>
                <div id="category-container" class="flex gap-2 overflow-x-auto pb-1 scrollbar-hide"></div>
            </div>
            <div id="product-list" class="p-4 grid grid-cols-2 gap-4"></div>
        </div>

        <div id="cart-tab" class="tab-content hidden p-4">
            <div class="flex justify-between items-center mb-4">
                <h2 class="font-bold text-gray-800 text-xl">🛒 သင့်ခြင်းတောင်း</h2>
                <button onclick="clearCart()" class="text-sm text-red-500 bg-red-50 hover:bg-red-100 px-3 py-1.5 rounded-lg transition-colors font-medium">အကုန်ဖျက်မည်</button>
            </div>
            
            <div id="cart-empty-state" class="empty-state hidden bg-white rounded-2xl shadow-sm border border-gray-100">
                <div class="empty-icon">🛍️</div>
                <p class="font-medium text-gray-800">ခြင်းတောင်းထဲတွင် ပစ္စည်းမရှိသေးပါ</p>
                <p class="text-sm mt-1">ဈေးဝယ်မည် ကိုနှိပ်ပြီး ပစ္စည်းများရွေးချယ်ပါ။</p>
                <button onclick="showTab('shop-tab', 'btn-shop')" class="mt-4 bg-blue-50 text-blue-600 px-4 py-2 rounded-lg font-bold">ဈေးဝယ်ရန် သွားမည်</button>
            </div>

            <div id="cart-content-wrapper">
                <div id="cart-items" class="space-y-3 mb-6"></div>
                
                <div class="bg-white p-5 rounded-2xl shadow-sm border border-gray-100">
                    <div class="flex justify-between items-center font-bold text-lg mb-4 border-b border-gray-100 pb-4">
                        <span class="text-gray-700">စုစုပေါင်း ကျသင့်ငွေ:</span> 
                        <span id="cart-total" class="text-blue-600 text-2xl">0 Ks</span>
                    </div>
                    
                    <div class="bg-blue-50 border border-blue-100 p-4 rounded-xl mb-5 text-sm text-blue-800 shadow-inner">
                        <p class="font-bold mb-2 flex items-center gap-1"><span>💳</span> ငွေပေးချေရန် အကောင့်များ</p>
                        <div class="grid grid-cols-1 gap-2">
                            <div class="bg-white p-2 rounded flex justify-between items-center"><span class="font-bold text-blue-900">KPay</span> <span id="pay-kpay" class="font-mono">Loading...</span></div>
                            <div class="bg-white p-2 rounded flex justify-between items-center"><span class="font-bold text-yellow-600">Wave</span> <span id="pay-wave" class="font-mono">Loading...</span></div>
                        </div>
                    </div>

                    <div class="bg-gray-50 border border-gray-200 p-4 rounded-xl mb-5">
                        <h3 class="font-bold text-gray-700 mb-3 text-sm flex items-center gap-1"><span>📍</span> ပို့ဆောင်ရမည့် လိပ်စာရွေးချယ်ရန်</h3>
                        
                        <div class="space-y-3">
                            <div>
                                <label class="block text-xs text-gray-500 mb-1">တိုင်းဒေသကြီး / ပြည်နယ်</label>
                                <select id="region-select" onchange="updateTownships()" class="w-full p-3 bg-white rounded-lg border border-gray-200 text-sm focus:ring-2 focus:ring-blue-500 outline-none transition-all appearance-none">
                                    <option value="">-- ရွေးချယ်ပါ --</option>
                                </select>
                            </div>
                            
                            <div>
                                <label class="block text-xs text-gray-500 mb-1">မြို့နယ်</label>
                                <select id="township-select" onchange="updateWards()" disabled class="w-full p-3 bg-white rounded-lg border border-gray-200 text-sm focus:ring-2 focus:ring-blue-500 outline-none transition-all appearance-none disabled:bg-gray-100 disabled:text-gray-400">
                                    <option value="">-- ရှေးဦးစွာ တိုင်း/ပြည်နယ် ရွေးပါ --</option>
                                </select>
                            </div>

                            <div>
                                <label class="block text-xs text-gray-500 mb-1">ရပ်ကွက် / ကျေးရွာ</label>
                                <select id="ward-select" disabled class="w-full p-3 bg-white rounded-lg border border-gray-200 text-sm focus:ring-2 focus:ring-blue-500 outline-none transition-all appearance-none disabled:bg-gray-100 disabled:text-gray-400">
                                    <option value="">-- ရှေးဦးစွာ မြို့နယ် ရွေးပါ --</option>
                                </select>
                            </div>

                            <div>
                                <label class="block text-xs text-gray-500 mb-1">အိမ်အမှတ် နှင့် လမ်းအမည်</label>
                                <input type="text" id="street-input" placeholder="ဥပမာ - အမှတ် (၁၅)၊ ဗိုလ်ချုပ်လမ်း..." class="w-full p-3 bg-white rounded-lg border border-gray-200 text-sm focus:ring-2 focus:ring-blue-500 outline-none transition-all">
                            </div>

                            <div>
                                <label class="block text-xs text-gray-500 mb-1">ဆက်သွယ်ရန် ဖုန်းနံပါတ်</label>
                                <input type="tel" id="phone-input" placeholder="ဥပမာ - 09123456789" class="w-full p-3 bg-white rounded-lg border border-gray-200 text-sm focus:ring-2 focus:ring-blue-500 outline-none transition-all">
                            </div>
                        </div>
                    </div>
                    <label class="block text-xs font-bold text-gray-600 mb-1.5 ml-1">ငွေလွှဲပြေစာ အမှတ် (Tx ID)</label>
                    <input type="text" id="checkout-tx" placeholder="နောက်ဆုံးဂဏန်း ၆ လုံး (သို့) ငွေလွှဲသူအမည်..." class="w-full p-3 mb-6 bg-gray-50 rounded-xl border border-gray-200 text-sm focus:ring-2 focus:ring-blue-500 outline-none transition-all">
                    
                    <button onclick="checkoutCart()" class="w-full bg-blue-600 hover:bg-blue-700 active:bg-blue-800 text-white py-4 rounded-xl font-bold transition-all shadow-[0_4px_14px_0_rgba(37,99,235,0.39)] text-lg flex justify-center items-center gap-2">
                        <span>အတည်ပြုပြီး အော်ဒါတင်မည်</span>
                    </button>
                </div>
            </div>
        </div>

        <div id="history-tab" class="tab-content hidden p-4">
            <h2 class="font-bold text-gray-800 text-xl mb-4">သင်၏ အော်ဒါမှတ်တမ်းများ</h2>
            <div id="buyer-order-list" class="space-y-3"></div>
        </div>
        
        <div id="orders-tab" class="tab-content hidden p-4">
            <div class="flex bg-gray-200 p-1 rounded-xl mb-4 shadow-inner">
                <button onclick="switchVendorTab('dash')" id="v-tab-dash" class="flex-1 bg-white shadow-sm py-2 rounded-lg text-sm font-bold text-gray-800 transition-all">အော်ဒါများ</button>
                <button onclick="switchVendorTab('prods')" id="v-tab-prods" class="flex-1 py-2 rounded-lg text-sm font-bold text-gray-500 hover:text-gray-700 transition-all">ပစ္စည်း စီမံရန်</button>
            </div>
            <div id="vendor-dash-view"><div id="order-list" class="space-y-3"></div></div>
            <div id="vendor-prods-view" class="hidden"><div id="vendor-product-list" class="space-y-3"></div></div>
        </div>

        <div id="toast">Message</div>

        <script>
            const tg = window.Telegram.WebApp;
            const initData = tg.initData; 
            let allProducts = [], currentCategory = 'All', cart = [];
            let searchTimeout = null;

            // ==========================================
            // 📍 MYANMAR LOCATION DATA (Dropdown တွက်)
            // ==========================================
            const mmLocations = {
                "ရန်ကုန်တိုင်းဒေသကြီး": {
                    "ကမာရွတ်မြို့နယ်": ["အမှတ်(၁) ရပ်ကွက်", "အမှတ်(၂) ရပ်ကွက်", "အမှတ်(၃) ရပ်ကွက်", "ဆင်မလိုက်ရပ်ကွက်"],
                    "လှိုင်မြို့နယ်": ["အမှတ်(၁) ရပ်ကွက်", "အမှတ်(၂) ရပ်ကွက်", "ဘူတာရုံရပ်ကွက်"],
                    "စမ်းချောင်းမြို့နယ်": ["မြေနီကုန်း", "ရှင်စောပု", "မုန့်လက်ဆောင်းကုန်း", "ကျွန်းတော"],
                    "လှည်းကူးမြို့နယ်": ["မြို့မရပ်ကွက်", "ဒါးပိန်ကျေးရွာ", "ဖောင်ကြီးကျေးရွာ"]
                },
                "မန္တလေးတိုင်းဒေသကြီး": {
                    "ချမ်းအေးသာစံမြို့နယ်": ["မြို့မ", "ပတ်ကုန်း", "ဟေမာဇလ"],
                    "မဟာအောင်မြေမြို့နယ်": ["မဟာမြိုင်", "စိန်ပန်း", "တံခွန်တိုင်"],
                    "ပြင်ဦးလွင်မြို့နယ်": ["ရပ်ကွက်ကြီး(၁)", "ရပ်ကွက်ကြီး(၂)", "အနီးစခန်းကျေးရွာ", "ပွဲကောက်ကျေးရွာ"]
                },
                "ရှမ်းပြည်နယ်": {
                    "တောင်ကြီးမြို့နယ်": ["ကျောင်းကြီးစု", "ကံသာ", "မြို့မ", "အေးသာယာ"],
                    "လားရှိုးမြို့နယ်": ["ရပ်ကွက်(၁)", "ရပ်ကွက်(၂)", "ရပ်ကွက်(၃)", "ရပ်ကွက်(၄)"]
                },
                "ပဲခူးတိုင်းဒေသကြီး": {
                    "ပဲခူးမြို့နယ်": ["ဟင်္သာသာ", "ကလျာဏီ", "ဥဿာမြို့သစ်"],
                    "တောင်ငူမြို့နယ်": ["မြို့မ(၁)", "မြို့မ(၂)", "ကေတုမတီ"]
                },
                "ဧရာဝတီတိုင်းဒေသကြီး": {
                    "ပုသိမ်မြို့နယ်": ["အမှတ်(၁) ရပ်ကွက်", "အမှတ်(၂) ရပ်ကွက်", "ရွှေမုဋ္ဌော"],
                    "ဟင်္သာတမြို့နယ်": ["တာကလေး", "မြို့မ", "နတ်မော်"]
                }
            };

            function initLocationSelectors() {
                const regionSelect = document.getElementById("region-select");
                regionSelect.innerHTML = '<option value="">-- တိုင်း/ပြည်နယ် ရွေးပါ --</option>';
                for (let region in mmLocations) {
                    regionSelect.innerHTML += `<option value="${region}">${region}</option>`;
                }
            }

            function updateTownships() {
                const region = document.getElementById("region-select").value;
                const townshipSelect = document.getElementById("township-select");
                const wardSelect = document.getElementById("ward-select");
                
                townshipSelect.innerHTML = '<option value="">-- မြို့နယ် ရွေးပါ --</option>';
                wardSelect.innerHTML = '<option value="">-- ရှေးဦးစွာ မြို့နယ် ရွေးပါ --</option>';
                wardSelect.disabled = true;

                if (region && mmLocations[region]) {
                    townshipSelect.disabled = false;
                    for (let township in mmLocations[region]) {
                        townshipSelect.innerHTML += `<option value="${township}">${township}</option>`;
                    }
                } else {
                    townshipSelect.disabled = true;
                }
            }

            function updateWards() {
                const region = document.getElementById("region-select").value;
                const township = document.getElementById("township-select").value;
                const wardSelect = document.getElementById("ward-select");
                
                wardSelect.innerHTML = '<option value="">-- ရပ်ကွက် / ကျေးရွာ ရွေးပါ --</option>';

                if (region && township && mmLocations[region][township]) {
                    wardSelect.disabled = false;
                    mmLocations[region][township].forEach(ward => {
                        wardSelect.innerHTML += `<option value="${ward}">${ward}</option>`;
                    });
                } else {
                    wardSelect.disabled = true;
                }
            }
            // ==========================================


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
                initLocationSelectors(); // Initialize Location UI

                try {
                    const res = await apiFetch('/api/auth');
                    const data = await res.json();
                    document.getElementById('display-name').innerText = data.user.name;
                    
                    // Populate previously saved phone number if exists
                    if(data.user.phone) {
                        document.getElementById('phone-input').value = data.user.phone;
                    }
                    
                    if(data.payment_info) {
                        document.getElementById('pay-kpay').innerText = data.payment_info.kpay;
                        document.getElementById('pay-wave').innerText = data.payment_info.wave;
                    }

                    if (['vendor', 'admin'].includes(data.user.role)) document.getElementById('btn-orders').classList.remove('hidden');
                    loadProducts();
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
                document.getElementById('product-list').innerHTML = '<div class="col-span-2 text-center text-gray-400 py-10">Loading...</div>';
                const res = await apiFetch(`/api/products?category=${currentCategory}&search=${query}`);
                const data = await res.json();
                allProducts = data.products;
                
                if(query === "") {
                    let catsHTML = `<button onclick="filterCategory('All')" class="cat-chip ${currentCategory==='All'?'active':''} whitespace-nowrap px-4 py-2 rounded-full border border-gray-200 text-sm font-medium transition-colors bg-white">အားလုံး</button>`;
                    data.categories.forEach(c => {
                        catsHTML += `<button onclick="filterCategory('${c}')" class="cat-chip ${currentCategory===c?'active':''} whitespace-nowrap px-4 py-2 rounded-full border border-gray-200 text-sm font-medium transition-colors bg-white">${c}</button>`;
                    });
                    document.getElementById('category-container').innerHTML = catsHTML;
                }
                renderProducts(allProducts);
            }

            function filterCategory(cat) {
                if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
                currentCategory = cat; document.getElementById('search-box').value = "";
                loadProducts();
            }

            function autoSearch() {
                clearTimeout(searchTimeout);
                searchTimeout = setTimeout(() => { loadProducts(document.getElementById('search-box').value); }, 400);
            }

            function renderProducts(products) {
                if(products.length === 0) {
                    document.getElementById('product-list').innerHTML = `<div class="col-span-2 empty-state bg-white rounded-2xl shadow-sm border border-gray-100"><div class="empty-icon">🔍</div><p>ရှာဖွေမှုနှင့် ကိုက်ညီသော ပစ္စည်းမရှိပါ</p></div>`;
                    return;
                }
                document.getElementById('product-list').innerHTML = products.map(p => {
                    const imgSrc = p.img ? `/api/image/${p.img}` : 'https://via.placeholder.com/300?text=No+Image';
                    const isOut = p.stock <= 0;
                    return `
                    <div class="bg-white rounded-2xl shadow-[0_2px_8px_rgba(0,0,0,0.04)] border border-gray-100 overflow-hidden flex flex-col relative transition-transform active:scale-95 ${isOut ? 'opacity-60 grayscale-[50%]' : ''}">
                        ${isOut ? '<div class="absolute top-2 right-2 bg-red-500 text-white text-[10px] font-bold px-2 py-1 rounded shadow-sm z-10">ကုန်နေပါသည်</div>' : ''}
                        <div class="relative"><img src="${imgSrc}" class="w-full h-40 object-cover border-b border-gray-50"></div>
                        <div class="p-3 flex-grow flex flex-col justify-between bg-white">
                            <div>
                                <div class="text-[13px] font-bold text-gray-800 line-clamp-2 leading-tight">${p.name}</div>
                                <div class="text-blue-600 text-[15px] font-black mt-1.5">${p.price.toLocaleString()} Ks</div>
                            </div>
                            <button onclick="addToCart(${p.id}, '${p.name.replace(/'/g, "\\'")}', ${p.price}, ${p.stock})" class="mt-3 w-full ${isOut?'bg-gray-100 text-gray-400':'bg-blue-50 text-blue-700 hover:bg-blue-100'} py-2.5 rounded-xl font-bold text-sm transition-colors flex justify-center items-center gap-1" ${isOut?'disabled':''}>
                                🛒 <span>ထည့်မည်</span>
                            </button>
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
            
            function updateCartBadge() { 
                const b = document.getElementById('cart-count'); 
                let t = cart.reduce((s, i) => s + i.qty, 0); b.innerText = t; 
                t > 0 ? b.classList.remove('hidden') : b.classList.add('hidden'); 
            }
            
            function renderCart() {
                if(cart.length === 0) {
                    document.getElementById('cart-empty-state').classList.remove('hidden');
                    document.getElementById('cart-content-wrapper').classList.add('hidden');
                    return;
                }
                
                document.getElementById('cart-empty-state').classList.add('hidden');
                document.getElementById('cart-content-wrapper').classList.remove('hidden');

                let total = 0;
                document.getElementById('cart-items').innerHTML = cart.map((i, index) => {
                    total += (i.price * i.qty);
                    return `
                    <div class="flex justify-between items-center bg-white p-3.5 rounded-2xl border border-gray-100 shadow-sm">
                        <div class="flex-1 pr-2">
                            <div class="text-sm font-bold text-gray-800 line-clamp-1">${i.name}</div>
                            <div class="text-blue-600 font-bold mt-1 text-[15px]">${(i.price).toLocaleString()} Ks</div>
                        </div>
                        <div class="flex items-center gap-3 bg-gray-50 rounded-xl p-1 border border-gray-200 shadow-inner">
                            <button onclick="changeQty(${index}, -1)" class="w-8 h-8 flex items-center justify-center font-bold text-gray-600 bg-white shadow-sm rounded-lg active:bg-gray-100 transition">-</button>
                            <span class="font-bold text-sm min-w-[20px] text-center">${i.qty}</span>
                            <button onclick="changeQty(${index}, 1)" class="w-8 h-8 flex items-center justify-center font-bold text-gray-600 bg-white shadow-sm rounded-lg active:bg-gray-100 transition">+</button>
                        </div>
                    </div>`;
                }).join('');
                document.getElementById('cart-total').innerText = `${total.toLocaleString()} Ks`;
            }

            function changeQty(index, delta) {
                let item = cart[index];
                if(delta > 0 && item.qty >= item.maxStock) return showToast("လက်ကျန် မလုံလောက်ပါ။");
                item.qty += delta;
                if(item.qty <= 0) cart.splice(index, 1);
                updateCartBadge(); renderCart();
            }

            async function checkoutCart() {
                if(cart.length === 0) return;
                
                // Get structured address data
                const region = document.getElementById('region-select').value;
                const township = document.getElementById('township-select').value;
                const ward = document.getElementById('ward-select').value;
                const street = document.getElementById('street-input').value.trim();
                const phone = document.getElementById('phone-input').value.trim();
                const tx_id = document.getElementById('checkout-tx').value.trim();

                if(!region || !township || !ward || !street || !phone) {
                    return showToast("လိပ်စာနှင့် ဖုန်းနံပါတ် အချက်အလက်များကို အပြည့်အစုံ ဖြည့်ပေးပါ။");
                }
                if(!tx_id) return showToast("ငွေလွှဲပြေစာ အမှတ် (Tx ID) ထည့်သွင်းပေးပါ။");

                // Compile final address string
                const compiledAddress = `${street}၊ ${ward}၊ ${township}၊ ${region}။ (ဖုန်း - ${phone})`;

                tg.MainButton.showProgress();
                try {
                    const payload = { 
                        transaction_id: tx_id, 
                        address: compiledAddress, 
                        phone: phone,
                        cart: cart.map(i=>({id:i.id, qty:i.qty})) 
                    };
                    const res = await apiFetch(`/api/checkout`, { method: 'POST', body: JSON.stringify(payload) });
                    if(res.ok) { 
                        clearCart(); document.getElementById('checkout-tx').value = '';
                        showToast("✅ အော်ဒါတင်ခြင်း အောင်မြင်ပါသည်။"); 
                        showTab('history-tab', 'btn-history'); 
                    } else { const err = await res.json(); showToast(err.detail || "Error Occurred"); }
                } catch(e) { showToast("ဆက်သွယ်မှု ပြတ်တောက်သွားပါသည်။"); }
                finally { tg.MainButton.hideProgress(); }
            }

            function clearCart() { cart = []; updateCartBadge(); renderCart(); }

            // ================== BUYER HISTORY ==================
            const statusNames = { 'pending': 'စစ်ဆေးဆဲ ⏳', 'approved': 'ထုပ်ပိုးဆဲ 📦', 'shipped': 'ပို့ဆောင်လိုက်ပြီ 🚚', 'delivered': 'ရောက်ရှိပါပြီ ✅', 'cancelled': 'ပယ်ဖျက်လိုက်သည် ❌' };
            
            async function loadBuyerOrders() {
                document.getElementById('buyer-order-list').innerHTML = '<div class="text-center text-gray-400 py-10">Loading...</div>';
                const res = await apiFetch('/api/buyer/orders');
                const orders = await res.json();
                if(orders.length === 0) return document.getElementById('buyer-order-list').innerHTML = `<div class="empty-state bg-white rounded-2xl shadow-sm border border-gray-100"><div class="empty-icon">🧾</div><p>အော်ဒါမှတ်တမ်း မရှိသေးပါ။</p></div>`;
                
                document.getElementById('buyer-order-list').innerHTML = orders.map(o => `
                    <div class="bg-white p-4 rounded-2xl shadow-sm border border-gray-100 relative overflow-hidden">
                        <div class="absolute left-0 top-0 bottom-0 w-1 status-${o.status}"></div>
                        <div class="flex justify-between items-start mb-3 pl-2">
                            <span class="text-[13px] font-bold text-gray-800 pr-2">${o.name} <span class="text-blue-500 bg-blue-50 px-1.5 py-0.5 rounded text-[11px] ml-1">x${o.qty}</span></span>
                            <span class="text-[11px] font-bold px-2.5 py-1 rounded-md status-${o.status} whitespace-nowrap">${statusNames[o.status]}</span>
                        </div>
                        <div class="flex justify-between items-center text-xs pl-2">
                            <span class="text-gray-500">${o.date}</span>
                            <span class="font-black text-gray-800 text-[15px]">${(o.price * o.qty).toLocaleString()} Ks</span>
                        </div>
                        ${o.status === 'pending' ? `<button onclick="cancelOrder(${o.id})" class="mt-3 w-full bg-red-50 text-red-600 hover:bg-red-100 py-2 rounded-lg text-xs font-bold transition-colors">အော်ဒါ ပြန်လည်ပယ်ဖျက်မည်</button>` : ''}
                    </div>`).join('');
            }

            async function cancelOrder(orderId) {
                if(!confirm("ဤအော်ဒါကို ဖျက်သိမ်းမှာ သေချာပါသလား?")) return;
                const res = await apiFetch(`/api/buyer/orders/${orderId}/cancel`, {method: 'POST'});
                if(res.ok) { showToast("အော်ဒါ ဖျက်သိမ်းပြီးပါပြီ။"); loadBuyerOrders(); }
                else { showToast("ဖျက်သိမ်း၍ မရနိုင်ပါ။"); }
            }
            
            // ================== VENDOR MANAGEMENT ==================
            function switchVendorTab(tab) {
                document.getElementById('v-tab-dash').className = tab === 'dash' ? 'flex-1 bg-white shadow-sm py-2 rounded-lg text-sm font-bold text-gray-800 transition-all' : 'flex-1 py-2 rounded-lg text-sm font-bold text-gray-500 transition-all';
                document.getElementById('v-tab-prods').className = tab === 'prods' ? 'flex-1 bg-white shadow-sm py-2 rounded-lg text-sm font-bold text-gray-800 transition-all' : 'flex-1 py-2 rounded-lg text-sm font-bold text-gray-500 transition-all';
                
                document.getElementById('vendor-dash-view').style.display = tab === 'dash' ? 'block' : 'none';
                document.getElementById('vendor-prods-view').style.display = tab === 'prods' ? 'block' : 'none';
                if(tab === 'dash') loadVendorOrders(); else loadVendorProducts();
            }

            async function loadVendorOrders() {
                document.getElementById('order-list').innerHTML = '<div class="text-center text-gray-400 py-10">Loading...</div>';
                const res = await apiFetch('/api/vendor/orders');
                const orders = await res.json();
                if(orders.length === 0) return document.getElementById('order-list').innerHTML = `<div class="empty-state bg-white rounded-2xl border"><div class="empty-icon">📫</div><p>ဝင်ထားသော အော်ဒါမရှိသေးပါ။</p></div>`;

                document.getElementById('order-list').innerHTML = orders.map(o => `
                    <div class="bg-white p-4 rounded-2xl shadow-sm border border-gray-100 mb-3 relative overflow-hidden">
                        <div class="absolute left-0 top-0 bottom-0 w-1 status-${o.status}"></div>
                        <div class="flex justify-between mb-3 pl-2">
                            <div class="text-sm font-bold text-gray-800 pr-2">${o.name} <span class="text-blue-500 bg-blue-50 px-1.5 py-0.5 rounded text-xs ml-1">x${o.qty}</span></div>
                            <div class="text-[10px] uppercase font-bold status-${o.status} px-2 py-1 rounded whitespace-nowrap">${o.status}</div>
                        </div>
                        <div class="bg-gray-50 p-3 rounded-xl text-[13px] text-gray-700 mb-3 border border-gray-200 ml-2">
                            <div class="mb-1"><span class="font-bold text-gray-500">ဝယ်သူ:</span> <span class="font-medium">${o.buyer}</span></div>
                            <div class="mb-1"><span class="font-bold text-gray-500">လိပ်စာ:</span> <span class="font-medium">${o.addr}</span></div>
                            <div><span class="font-bold text-gray-500">Tx ID:</span> <span class="text-blue-600 font-mono font-bold bg-blue-50 px-1 rounded">${o.tx}</span></div>
                        </div>
                        <div class="flex gap-2 pl-2">
                            <select onchange="updateOrderStatus(${o.id}, this.value)" class="flex-1 bg-gray-50 border border-gray-200 text-[13px] font-bold p-2.5 rounded-xl outline-none focus:ring-2 focus:ring-blue-500">
                                <option value="pending" ${o.status==='pending'?'selected':''}>⏳ စစ်ဆေးဆဲ</option>
                                <option value="approved" ${o.status==='approved'?'selected':''}>📦 ငွေမှန်ကန် (ထုပ်ပိုးမည်)</option>
                                <option value="shipped" ${o.status==='shipped'?'selected':''}>🚚 ပို့ဆောင်လိုက်ပြီ</option>
                                <option value="delivered" ${o.status==='delivered'?'selected':''}>✅ ရောက်ရှိပါပြီ</option>
                                <option value="cancelled" ${o.status==='cancelled'?'selected':''}>❌ ပယ်ဖျက်မည်</option>
                            </select>
                        </div>
                    </div>`).join('');
            }

            async function updateOrderStatus(orderId, newStatus) {
                const res = await apiFetch(`/api/vendor/orders/${orderId}/status?status=${newStatus}`, {method:'POST'});
                if(res.ok) { showToast("အခြေအနေ ပြောင်းလဲပြီးပါပြီ"); loadVendorOrders(); }
            }

            async function loadVendorProducts() {
                document.getElementById('vendor-product-list').innerHTML = '<div class="text-center text-gray-400 py-10">Loading...</div>';
                const res = await apiFetch('/api/vendor/products');
                const prods = await res.json();
                if(prods.length === 0) return document.getElementById('vendor-product-list').innerHTML = `<div class="empty-state bg-white rounded-2xl border"><div class="empty-icon">📦</div><p>တင်ထားသော ပစ္စည်းမရှိပါ။</p></div>`;

                document.getElementById('vendor-product-list').innerHTML = prods.map(p => `
                    <div class="bg-white p-4 rounded-2xl shadow-sm border border-gray-100 flex flex-col gap-3 mb-3">
                        <div class="flex justify-between items-start">
                            <div>
                                <div class="text-[14px] font-bold text-gray-800 leading-tight mb-1">${p.name}</div>
                                <div class="text-blue-600 font-bold text-[13px]">${p.price.toLocaleString()} Ks</div>
                            </div>
                            <div class="flex flex-col gap-1.5 ml-2">
                                <button onclick="editProduct(${p.id}, '${p.name.replace(/'/g, "\\'")}', ${p.price})" class="text-blue-600 text-xs font-bold bg-blue-50 hover:bg-blue-100 px-3 py-1.5 rounded-lg transition-colors">ပြင်မည်</button>
                                <button onclick="if(confirm('ဖျက်မှာသေချာပါသလား?')) apiFetch('/api/vendor/products/${p.id}', {method:'DELETE'}).then(loadVendorProducts)" class="text-red-500 text-xs font-bold bg-red-50 hover:bg-red-100 px-3 py-1.5 rounded-lg transition-colors">ဖျက်မည်</button>
                            </div>
                        </div>
                        <div class="flex justify-between items-center bg-gray-50 p-2.5 rounded-xl border border-gray-200">
                            <span class="text-xs font-bold text-gray-600">လက်ကျန် (Stock):</span>
                            <div class="flex items-center gap-3">
                                <button onclick="updateStock(${p.id}, ${p.stock - 1})" class="w-8 h-8 bg-white border border-gray-200 rounded-lg flex items-center justify-center font-bold text-gray-600 shadow-sm active:bg-gray-100 transition">-</button>
                                <span class="font-bold text-[15px] min-w-[24px] text-center">${p.stock}</span>
                                <button onclick="updateStock(${p.id}, ${p.stock + 1})" class="w-8 h-8 bg-white border border-gray-200 rounded-lg flex items-center justify-center font-bold text-gray-600 shadow-sm active:bg-gray-100 transition">+</button>
                            </div>
                        </div>
                    </div>`).join('');
            }

            async function editProduct(productId, oldName, oldPrice) {
                const newName = prompt("ပစ္စည်းအမည် အသစ်ရိုက်ထည့်ပါ:", oldName);
                if(newName === null) return;
                const newPriceStr = prompt("ဈေးနှုန်းအသစ် ရိုက်ထည့်ပါ (ဂဏန်းသီးသန့်):", oldPrice);
                if(newPriceStr === null) return;
                const newPrice = parseFloat(newPriceStr);
                
                if(newName.trim() === '' || isNaN(newPrice)) return showToast("အချက်အလက် မှားယွင်းနေပါသည်။");
                
                const res = await apiFetch(`/api/vendor/products/${productId}/edit`, { method: 'PUT', body: JSON.stringify({name: newName, price: newPrice}) });
                if(res.ok) { showToast("ပြင်ဆင်ပြီးပါပြီ"); loadVendorProducts(); }
            }

            async function updateStock(productId, newStock) {
                if(newStock < 0) return;
                await apiFetch(`/api/vendor/products/${productId}/stock`, { method: 'PUT', body: JSON.stringify({stock: newStock}) });
                loadVendorProducts();
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
