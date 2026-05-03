import os
import hmac
import hashlib
import json
import threading
import datetime
import time
import requests
import base64
from io import BytesIO
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

# ဆိုင်ရှင်/Admin ၏ ငွေလက်ခံမည့် အချက်အလက်များ
PAYMENT_INFO = {
    "kpay": "09123456789 (Digital Mall)",
    "wave": "09123456789 (Digital Mall)",
    "qr_url": "https://api.qrserver.com/v1/create-qr-code/?size=150x150&data=09123456789" 
}

bot = TeleBot(BOT_TOKEN)
app = FastAPI(title="Digital Mall Auto-Run System Pro")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_headers=["*"], allow_methods=["*"])

try:
    if REDIS_URL:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
except: 
    redis_client = None

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
    payment_method = Column(String, default="COD") 
    transaction_id = Column(String, default="") 
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
    return {
        "user": {
            "id": user.telegram_id, "name": user.full_name, "role": user.role, 
            "default_address": user.default_address, "phone": user.phone
        }, "payment_info": PAYMENT_INFO
    }

# အလိုအလျောက် လိပ်စာ သိမ်းဆည်းရန်နှင့် Vendor အဖြစ်မြှင့်တင်ရန်
@app.post("/api/user/address")
async def update_user_address(req: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await req.json()
    if "address" in data: user.default_address = data["address"]
    if "phone" in data: user.phone = data["phone"]
    if user.role == "buyer": user.role = "vendor"
    db.commit()
    return {"status": "success"}

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
    payment_method = data.get('payment_method', 'COD')
    tx_id = data.get('transaction_id', '')
    address = data.get('address', 'Unknown')
    phone = data.get('phone', '')
    receipt_b64 = data.get('receipt_b64', '')

    if not cart_items: raise HTTPException(status_code=400, detail="Cart is empty")
    total_amount, ordered_names = 0, []
    vendors_to_notify = {}

    for item in cart_items:
        p_id = item.get('id')
        qty = item.get('qty', 1)
        product = db.query(Product).filter(Product.id == p_id).with_for_update().first()
        
        if product and product.stock >= qty:
            db.add(Order(user_id=user.id, product_id=product.id, quantity=qty, payment_method=payment_method, transaction_id=tx_id, address=address))
            product.stock -= qty 
            total_amount += (product.price * qty)
            ordered_names.append(f"{product.name} (x{qty})")
            
            if product.vendor: 
                v_id = product.vendor.telegram_id
                if v_id not in vendors_to_notify: vendors_to_notify[v_id] = []
                vendors_to_notify[v_id].append(f"{product.name} (x{qty})")
        else:
            db.rollback()
            raise HTTPException(status_code=400, detail=f"'{product.name if product else 'Item'}' ပစ္စည်းလက်ကျန်မလုံလောက်ပါ။")
            
    if address and user.default_address != address: user.default_address = address
    if phone and user.phone != phone: user.phone = phone
    db.commit()

    img_data = None
    if receipt_b64 and "," in receipt_b64:
        try:
            encoded = receipt_b64.split(",", 1)[1]
            img_data = base64.b64decode(encoded)
        except: pass

    try:
        items_str = "\n".join([f"- {n}" for n in ordered_names])
        pay_str = "အိမ်ရောက်မှ ငွေချေစနစ် (COD)" if payment_method == "COD" else f"Mobile Pay (Tx: {tx_id})"
        
        bot.send_message(user.telegram_id, f"🛒 **အော်ဒါ လက်ခံရရှိပါသည်**\n\n{items_str}\n\nစုစုပေါင်း: {total_amount:,.0f} Ks\nငွေချေစနစ်: {pay_str}\nပို့ဆောင်ရမည့်လိပ်စာ: {address}", parse_mode="Markdown")
        
        for v_tg_id, p_list in vendors_to_notify.items():
            v_items = "\n".join([f"- {n}" for n in p_list])
            notify_msg = f"🔔 **အော်ဒါအသစ်ဝင်ပါသည်**\nဝယ်သူ: {user.full_name}\nလိပ်စာ: {address}\nဖုန်း: {phone}\nငွေချေစနစ်: {pay_str}\n\nပစ္စည်းများ:\n{v_items}"
            if img_data and payment_method == "QR":
                bot.send_photo(v_tg_id, BytesIO(img_data), caption=notify_msg)
            else:
                bot.send_message(v_tg_id, notify_msg, parse_mode="Markdown")
    except: pass
    return {"status": "success"}

@app.get("/api/buyer/orders")
def get_buyer_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    orders = db.query(Order).filter(Order.user_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "qty": o.quantity, "price": o.product.price, "status": o.status, "pay": o.payment_method, "date": o.created_at.strftime("%Y-%m-%d")} for o in orders]

@app.post("/api/buyer/orders/{order_id}/cancel")
def cancel_buyer_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id, Order.user_id == user.id).first()
    if not order or order.status != "pending": raise HTTPException(status_code=400)
    order.status = "cancelled"
    order.product.stock += order.quantity 
    db.commit()
    return {"status": "success"}

@app.get("/api/vendor/orders")
def get_vendor_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    orders = db.query(Order).join(Product).filter(Product.vendor_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "qty": o.quantity, "buyer": o.user.full_name, "pay": o.payment_method, "tx": o.transaction_id, "addr": o.address, "status": o.status} for o in orders]

@app.post("/api/vendor/orders/{order_id}/status")
def update_order_status(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    status_map = {"approved": "✅ အတည်ပြုပါသည်။ ထုပ်ပိုးနေပါသည်။", "shipped": "🚚 ပစ္စည်းပို့ဆောင်ပေးလိုက်ပါပြီ။", "delivered": "🎁 ပစ္စည်းလက်ခံရရှိကြောင်း မှတ်တမ်းတင်ပြီးပါပြီ။", "cancelled": "❌ အော်ဒါအား ပယ်ဖျက်လိုက်ပါသည်။"}
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or (order.product.vendor_id != user.id and user.role != "admin"): raise HTTPException(status_code=400)
    
    new_status = request.query_params.get("status")
    if new_status not in status_map: raise HTTPException(status_code=400)
    if new_status == "cancelled" and order.status != "cancelled": order.product.stock += order.quantity 
    order.status = new_status
    db.commit()
    try: bot.send_message(order.user.telegram_id, f"{status_map[new_status]}\nပစ္စည်း: **{order.product.name} (x{order.quantity})**", parse_mode="Markdown")
    except: pass
    return {"status": "success"}

@app.get("/api/vendor/products")
def get_vendor_products(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    products = db.query(Product).filter(Product.vendor_id == user.id).order_by(Product.id.desc()).all()
    return [{"id":p.id, "name":p.name, "price":p.price, "stock":p.stock} for p in products]

@app.put("/api/vendor/products/{product_id}/stock")
async def update_product_stock(product_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await request.json()
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user.id).first()
    if product: product.stock = data.get("stock", product.stock); db.commit()
    return {"status": "success"}

@app.delete("/api/vendor/products/{product_id}")
def delete_product(product_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user.id).first()
    if product: db.delete(product); db.commit()
    return {"status": "success"}

# ==========================================
# ၅။ AI-POWERED CMS & CHAT BOT
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🏬 ကုန်တိုက်သို့ဝင်ရန်", web_app=types.WebAppInfo(WEBAPP_URL)))
    msg = """မင်္ဂလာပါရှင်။ \n🛍️ **ဈေးဝယ်လိုပါက** အောက်ပါခလုတ်ကို နှိပ်၍ ဝင်ရောက်နိုင်ပါသည်။\n📦 **မိမိပစ္စည်းများကို ရောင်းချလိုပါက** Web App ထဲရှိ 'ရောင်းမည်' ခလုတ်ကို အရင်နှိပ်၍ ပရိုဖိုင်း ဖွင့်လှစ်ပါ။ ပြီးပါက ပစ္စည်းဓာတ်ပုံနှင့်တကွ 'အမည် - ဈေးနှုန်း' ကိုပေးပို့ရုံဖြင့် AI မှ အလိုအလျောက် စာရင်းသွင်း ရောင်းချပေးမည် ဖြစ်ပါသည်။"""
    bot.send_message(message.chat.id, msg, reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(content_types=['photo'])
def handle_cms_photo(message):
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == str(message.from_user.id)).first()
    if not user or user.role not in ["vendor", "admin"]: 
        bot.reply_to(message, "⚠️ ကျေးဇူးပြု၍ ကုန်တိုက် App အတွင်းရှိ 'ရောင်းမည်' ခလုတ်ကို အရင်နှိပ်၍ သင့်တည်နေရာကို အတည်ပြုပေးပါ။")
        return db.close()

    try:
        caption = message.caption or "New Product"
        file_id = message.photo[-1].file_id 
        ai_data = {"name": caption.split('-')[0].strip() if '-' in caption else "New Product", "price": caption.split('-')[1].strip() if '-' in caption else 0, "category": "General", "description": caption, "stock": 10}
        
        if GROQ_API_KEY and '-' not in caption: 
            try:
                msg = bot.reply_to(message, "⏳ AI ဖြင့် ပစ္စည်းအချက်အလက်များကို ခွဲခြမ်းစိတ်ဖြာနေပါသည်...")
                headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
                prompt = f"""Analyze the Burmese text for an e-commerce product: "{caption}". Extract details to strictly JSON. Required keys: 'name', 'price' (numeric), 'category', 'description', 'stock' (numeric)."""
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
        bot.reply_to(message, f"✅ **ပစ္စည်း အလိုအလျောက် တင်ပြီးပါပြီ။**\n\n📌 အမည်: {ai_data['name']}\n💰 ဈေးနှုန်း: {ai_data['price']} Ks\n📦 အရေအတွက်: {ai_data['stock']}\n\n_App ထဲသို့ဝင်၍ 'စီမံရန်' Tab တွင် အလွယ်တကူ ထပ်မံပြင်ဆင်နိုင်ပါသည်။_", parse_mode="Markdown")
    except Exception as e: bot.reply_to(message, f"အမှားအယွင်း ဖြစ်ပေါ်ခဲ့ပါသည်။ ('အမည် - ဈေးနှုန်း' ပုံစံဖြင့် ရေးပို့ပေးပါ)")
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
        <title>Digital Mall Auto Pro</title>
        <style>
            body { font-family: sans-serif; -webkit-tap-highlight-color: transparent; background-color: #f3f4f6; }
            .tab-btn.active { color: #2563eb; border-bottom: 3px solid #2563eb; }
            .cat-chip.active { background-color: #2563eb; color: white; border-color: #2563eb; }
            .cart-badge { position: absolute; top: -2px; right: -2px; background: #ef4444; color: white; border-radius: 50%; padding: 2px 6px; font-size: 10px; font-weight: bold;}
            
            #toast { visibility: hidden; min-width: 250px; background-color: rgba(31, 41, 55, 0.95); color: #fff; text-align: center; border-radius: 12px; padding: 14px; position: fixed; z-index: 1000; left: 50%; bottom: 80px; transform: translateX(-50%); font-size: 14px; backdrop-filter: blur(4px); box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1); }
            #toast.show { visibility: visible; animation: fadein 0.3s, fadeout 0.3s 2.5s; }
            @keyframes fadein { from {bottom: 50px; opacity: 0;} to {bottom: 80px; opacity: 1;} }
            @keyframes fadeout { from {bottom: 80px; opacity: 1;} to {bottom: 50px; opacity: 0;} }
            
            /* Modal / Popup Styles */
            .modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 500; display: flex; justify-content: center; align-items: center; opacity: 0; pointer-events: none; transition: opacity 0.3s; }
            .modal-overlay.active { opacity: 1; pointer-events: auto; }
            .modal-content { background: white; width: 90%; max-width: 400px; border-radius: 20px; padding: 24px; transform: translateY(20px); transition: transform 0.3s; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1); }
            .modal-overlay.active .modal-content { transform: translateY(0); }

            .status-pending { background-color: #fef3c7; color: #d97706; }
            .status-approved { background-color: #dbeafe; color: #2563eb; }
            .status-shipped { background-color: #f3e8ff; color: #9333ea; }
            .status-delivered { background-color: #dcfce3; color: #166534; }
            .status-cancelled { background-color: #fee2e2; color: #dc2626; }
            
            /* Custom Radio Buttons */
            .pay-radio:checked + label { border-color: #2563eb; background-color: #eff6ff; }
            .pay-radio:checked + label .radio-dot { background-color: #2563eb; border-color: #2563eb; }
        </style>
    </head>
    <body class="pb-20">
        
        <header class="bg-white p-4 shadow-sm sticky top-0 z-40 flex justify-between items-center">
            <span class="font-bold text-blue-700 text-xl tracking-tight">Digital<span class="text-gray-800">Mall</span></span>
            <div class="flex items-center gap-3">
                <div class="text-xs bg-gray-100 px-3 py-1.5 rounded-full text-gray-700 font-medium max-w-[120px] truncate border border-gray-200" id="display-name">...</div>
                <button onclick="showTab('cart-tab', 'btn-shop')" class="relative p-2.5 rounded-full bg-blue-50 text-blue-600 active:bg-blue-100 transition">
                    🛒<span id="cart-count" class="cart-badge hidden">0</span>
                </button>
            </div>
        </header>

        <div class="fixed bottom-0 w-full bg-white border-t flex justify-around text-xs font-medium text-gray-500 z-40 pb-safe shadow-[0_-5px_10px_rgba(0,0,0,0.05)]">
            <button id="btn-shop" onclick="showTab('shop-tab', 'btn-shop')" class="tab-btn active flex-1 py-3 flex flex-col items-center gap-1 transition-colors">
                <span class="text-[20px]">🏠</span><span>ဝယ်မည်</span>
            </button>
            <button onclick="openSellModal()" class="tab-btn flex-1 py-3 flex flex-col items-center gap-1 transition-colors relative">
                <div class="absolute -top-3 bg-blue-600 text-white w-12 h-12 rounded-full flex items-center justify-center shadow-lg border-4 border-white text-2xl pb-1">+</div>
                <span class="mt-6 font-bold text-blue-600">ရောင်းမည်</span>
            </button>
            <button id="btn-history" onclick="showTab('history-tab', 'btn-history')" class="tab-btn flex-1 py-3 flex flex-col items-center gap-1 transition-colors">
                <span class="text-[20px]">📋</span><span>မှတ်တမ်း</span>
            </button>
            <button id="btn-orders" onclick="showTab('orders-tab', 'btn-orders')" class="tab-btn hidden flex-1 py-3 flex flex-col items-center gap-1 transition-colors">
                <span class="text-[20px]">⚙️</span><span>စီမံရန်</span>
            </button>
        </div>

        <div id="sell-modal" class="modal-overlay">
            <div class="modal-content text-center">
                <div class="text-4xl mb-3">🏪</div>
                <h3 class="text-lg font-bold text-gray-800 mb-2">ဆိုင်ရှင် ပရိုဖိုင်း ဖွင့်လှစ်ခြင်း</h3>
                <p class="text-sm text-gray-500 mb-5">ဝယ်သူများ ယုံကြည်မှုပိုရှိစေရန် သင်၏ တည်နေရာကို အလိုအလျောက် သတ်မှတ်ပေးပါမည်။</p>
                
                <button id="btn-get-location" onclick="fetchGPSLocation()" class="w-full bg-blue-100 text-blue-700 font-bold py-3 rounded-xl mb-3 flex items-center justify-center gap-2 transition active:bg-blue-200">
                    <span>📍</span> တည်နေရာကို အလိုအလျောက် ရယူမည်
                </button>
                
                <div id="location-result" class="hidden mb-4">
                    <input type="text" id="seller-address" placeholder="အိမ်လိပ်စာ အတိအကျ..." class="w-full p-3 bg-gray-50 border border-gray-200 rounded-lg text-sm mb-2 outline-none focus:ring-1 focus:ring-blue-500">
                    <input type="tel" id="seller-phone" placeholder="ဆက်သွယ်ရန် ဖုန်းနံပါတ်..." class="w-full p-3 bg-gray-50 border border-gray-200 rounded-lg text-sm outline-none focus:ring-1 focus:ring-blue-500">
                </div>

                <button id="btn-confirm-sell" onclick="confirmSellerProfile()" class="w-full bg-blue-600 text-white font-bold py-3 rounded-xl shadow-md transition active:bg-blue-700 hidden">
                    အတည်ပြုပြီး ပစ္စည်းတင်မည်
                </button>
                <button onclick="closeSellModal()" class="mt-4 text-sm font-bold text-gray-400">ပယ်ဖျက်မည်</button>
            </div>
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
            
            <div id="cart-empty-state" class="text-center py-10 bg-white rounded-2xl shadow-sm border border-gray-100 hidden">
                <div class="text-4xl mb-3 opacity-50">🛍️</div>
                <p class="font-medium text-gray-800">ခြင်းတောင်းထဲတွင် ပစ္စည်းမရှိပါ</p>
            </div>

            <div id="cart-content-wrapper">
                <div id="cart-items" class="space-y-3 mb-6"></div>
                
                <div class="bg-white p-5 rounded-2xl shadow-sm border border-gray-100">
                    <div class="flex justify-between items-center font-bold text-lg mb-4 border-b border-gray-100 pb-4">
                        <span class="text-gray-700">စုစုပေါင်း:</span> 
                        <span id="cart-total" class="text-blue-600 text-2xl">0 Ks</span>
                    </div>

                    <h3 class="font-bold text-gray-700 mb-3 text-sm flex items-center gap-1"><span>💳</span> ငွေပေးချေမည့် စနစ်ရွေးချယ်ရန်</h3>
                    <div class="grid grid-cols-2 gap-3 mb-5">
                        <div class="relative">
                            <input type="radio" name="pay_method" id="pay-cod" value="COD" class="pay-radio hidden" checked onchange="togglePaymentUI()">
                            <label for="pay-cod" class="flex flex-col items-center justify-center p-3 border-2 border-gray-200 rounded-xl cursor-pointer transition-all">
                                <span class="text-2xl mb-1">🚚</span>
                                <span class="text-[11px] font-bold text-gray-700">အိမ်ရောက်ငွေချေ</span>
                                <div class="radio-dot w-4 h-4 border-2 border-gray-300 rounded-full mt-2"></div>
                            </label>
                        </div>
                        <div class="relative">
                            <input type="radio" name="pay_method" id="pay-qr" value="QR" class="pay-radio hidden" onchange="togglePaymentUI()">
                            <label for="pay-qr" class="flex flex-col items-center justify-center p-3 border-2 border-gray-200 rounded-xl cursor-pointer transition-all">
                                <span class="text-2xl mb-1">📱</span>
                                <span class="text-[11px] font-bold text-gray-700">QR / Mobile Pay</span>
                                <div class="radio-dot w-4 h-4 border-2 border-gray-300 rounded-full mt-2"></div>
                            </label>
                        </div>
                    </div>

                    <div id="qr-payment-section" class="hidden bg-blue-50 border border-blue-100 p-4 rounded-xl mb-5 shadow-inner">
                        <div class="text-center mb-3">
                            <img id="qr-image" src="" alt="QR Code" class="mx-auto w-32 h-32 rounded-lg shadow-sm border border-gray-200">
                        </div>
                        <div class="space-y-2 mb-4 text-sm text-blue-900">
                            <div class="bg-white p-2 rounded flex justify-between"><b>KPay:</b> <span id="pay-kpay"></span></div>
                            <div class="bg-white p-2 rounded flex justify-between"><b>Wave:</b> <span id="pay-wave"></span></div>
                        </div>
                        
                        <label class="block text-xs font-bold text-gray-600 mb-1.5">ငွေလွှဲပြေစာ အမှတ် (Tx ID)</label>
                        <input type="text" id="checkout-tx" placeholder="ဂဏန်း ၆ လုံး..." class="w-full p-3 mb-3 bg-white rounded-lg border border-gray-200 text-sm outline-none">
                        
                        <label class="block text-xs font-bold text-gray-600 mb-1.5">ငွေလွှဲပြေစာ ဓာတ်ပုံတင်ရန်</label>
                        <input type="file" id="checkout-receipt" accept="image/*" class="w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-xs file:font-bold file:bg-blue-100 file:text-blue-700 hover:file:bg-blue-200 cursor-pointer bg-white border border-gray-200 rounded-lg p-1">
                    </div>

                    <h3 class="font-bold text-gray-700 mb-3 text-sm flex items-center gap-1"><span>📍</span> ပို့ဆောင်ရမည့် လိပ်စာ</h3>
                    <textarea id="checkout-address" placeholder="အိမ်အမှတ်၊ လမ်း၊ မြို့နယ် အတိအကျ..." rows="3" class="w-full p-3 bg-gray-50 rounded-xl border border-gray-200 text-sm focus:ring-2 focus:ring-blue-500 outline-none mb-3"></textarea>
                    
                    <input type="tel" id="checkout-phone" placeholder="ဆက်သွယ်ရန် ဖုန်းနံပါတ်..." class="w-full p-3 mb-6 bg-gray-50 rounded-xl border border-gray-200 text-sm focus:ring-2 focus:ring-blue-500 outline-none">

                    <button onclick="checkoutCart()" class="w-full bg-blue-600 hover:bg-blue-700 text-white py-4 rounded-xl font-bold transition-all text-lg flex justify-center items-center gap-2 shadow-lg">
                        အတည်ပြုပြီး အော်ဒါတင်မည်
                    </button>
                </div>
            </div>
        </div>

        <div id="history-tab" class="tab-content hidden p-4">
            <h2 class="font-bold text-gray-800 text-xl mb-4">သင်၏ အော်ဒါမှတ်တမ်းများ</h2>
            <div id="buyer-order-list" class="space-y-3"></div>
        </div>
        
        <div id="orders-tab" class="tab-content hidden p-4">
            <div class="flex bg-gray-200 p-1 rounded-xl mb-4">
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
                try {
                    const res = await apiFetch('/api/auth');
                    const data = await res.json();
                    document.getElementById('display-name').innerText = data.user.name;
                    if(data.user.phone) {
                        document.getElementById('checkout-phone').value = data.user.phone;
                        document.getElementById('seller-phone').value = data.user.phone;
                    }
                    if(data.user.default_address) {
                        document.getElementById('checkout-address').value = data.user.default_address;
                        document.getElementById('seller-address').value = data.user.default_address;
                    }
                    if(data.payment_info) {
                        document.getElementById('pay-kpay').innerText = data.payment_info.kpay;
                        document.getElementById('pay-wave').innerText = data.payment_info.wave;
                        document.getElementById('qr-image').src = data.payment_info.qr_url;
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

            // ================== SELLER AUTO-LOCATION ==================
            function openSellModal() {
                document.getElementById('sell-modal').classList.add('active');
                if(document.getElementById('seller-address').value !== "") {
                    document.getElementById('btn-get-location').classList.add('hidden');
                    document.getElementById('location-result').classList.remove('hidden');
                    document.getElementById('btn-confirm-sell').classList.remove('hidden');
                }
            }
            function closeSellModal() { document.getElementById('sell-modal').classList.remove('active'); }
            
            function fetchGPSLocation() {
                if (!navigator.geolocation) return showToast("ဖုန်းတွင် GPS ဖွင့်ရန် မရနိုင်ပါ။ ကိုယ်တိုင်ရိုက်ထည့်ပါ။");
                
                const btn = document.getElementById('btn-get-location');
                btn.innerHTML = "⏳ ရှာဖွေနေပါသည်..."; btn.disabled = true;

                navigator.geolocation.getCurrentPosition(async (pos) => {
                    const lat = pos.coords.latitude; const lon = pos.coords.longitude;
                    try {
                        const res = await fetch(`https://nominatim.openstreetmap.org/reverse?format=json&lat=${lat}&lon=${lon}&accept-language=my`);
                        const data = await res.json();
                        let address = data.display_name || "လိပ်စာ အတိအကျမရပါ။ ကိုယ်တိုင်ပြင်ဆင်ပါ။";
                        
                        document.getElementById('seller-address').value = address;
                        btn.classList.add('hidden');
                        document.getElementById('location-result').classList.remove('hidden');
                        document.getElementById('btn-confirm-sell').classList.remove('hidden');
                    } catch(e) { showToast("လိပ်စာရှာမရပါ။ ကိုယ်တိုင်ရိုက်ထည့်ပါ။"); }
                }, (err) => {
                    showToast("GPS ဖွင့်ခွင့်ပြုရန် လိုအပ်ပါသည်။ ကိုယ်တိုင်ရိုက်ထည့်ပါ။");
                    btn.classList.add('hidden');
                    document.getElementById('location-result').classList.remove('hidden');
                    document.getElementById('btn-confirm-sell').classList.remove('hidden');
                });
            }

            async function confirmSellerProfile() {
                const addr = document.getElementById('seller-address').value;
                const ph = document.getElementById('seller-phone').value;
                if(!addr || !ph) return showToast("လိပ်စာနှင့် ဖုန်းနံပါတ် ဖြည့်ပါ။");
                
                const res = await apiFetch('/api/user/address', { method: 'POST', body: JSON.stringify({address: addr, phone: ph}) });
                if(res.ok) {
                    closeSellModal();
                    document.getElementById('btn-orders').classList.remove('hidden');
                    tg.showConfirm("✅ အကောင့်ဖွင့်ပြီးပါပြီ။\n\nပစ္စည်းတင်ရန်အတွက် ဤ App ကိုပိတ်ပြီး၊ Bot ဆီသို့ ပစ္စည်းဓာတ်ပုံနှင့် 'အမည် - ဈေးနှုန်း' ကိုပေးပို့လိုက်ပါ။ အခုပဲ ပုံပို့မလား?", (res) => {
                        if(res) tg.close();
                    });
                }
            }

            // ================== PRODUCTS & CART ==================
            async function loadProducts(query = "") {
                document.getElementById('product-list').innerHTML = '<div class="col-span-2 text-center text-gray-400 py-10">Loading...</div>';
                const res = await apiFetch(`/api/products?category=${currentCategory}&search=${query}`);
                const data = await res.json();
                allProducts = data.products;
                if(query === "") {
                    let catsHTML = `<button onclick="filterCategory('All')" class="cat-chip ${currentCategory==='All'?'active':''} whitespace-nowrap px-4 py-2 rounded-full border border-gray-200 text-sm font-medium transition-colors bg-white">အားလုံး</button>`;
                    data.categories.forEach(c => catsHTML += `<button onclick="filterCategory('${c}')" class="cat-chip ${currentCategory===c?'active':''} whitespace-nowrap px-4 py-2 rounded-full border border-gray-200 text-sm font-medium transition-colors bg-white">${c}</button>`);
                    document.getElementById('category-container').innerHTML = catsHTML;
                }
                renderProducts(allProducts);
            }

            function filterCategory(cat) { currentCategory = cat; document.getElementById('search-box').value = ""; loadProducts(); }
            function autoSearch() { clearTimeout(searchTimeout); searchTimeout = setTimeout(() => { loadProducts(document.getElementById('search-box').value); }, 400); }

            function renderProducts(products) {
                if(products.length === 0) return document.getElementById('product-list').innerHTML = `<div class="col-span-2 text-center text-gray-400 py-10">ပစ္စည်းမရှိပါ</div>`;
                document.getElementById('product-list').innerHTML = products.map(p => {
                    const imgSrc = p.img ? `/api/image/${p.img}` : 'https://via.placeholder.com/300?text=No+Image';
                    const isOut = p.stock <= 0;
                    return `
                    <div class="bg-white rounded-2xl shadow-[0_2px_8px_rgba(0,0,0,0.04)] border border-gray-100 overflow-hidden flex flex-col ${isOut ? 'opacity-60 grayscale-[50%]' : ''}">
                        ${isOut ? '<div class="absolute bg-red-500 text-white text-[10px] font-bold px-2 py-1 rounded shadow-sm z-10 m-2">ကုန်နေပါသည်</div>' : ''}
                        <img src="${imgSrc}" class="w-full h-40 object-cover border-b border-gray-50">
                        <div class="p-3 flex-grow flex flex-col justify-between">
                            <div>
                                <div class="text-[13px] font-bold text-gray-800 line-clamp-2 leading-tight">${p.name}</div>
                                <div class="text-blue-600 text-[15px] font-black mt-1.5">${p.price.toLocaleString()} Ks</div>
                            </div>
                            <button onclick="addToCart(${p.id}, '${p.name.replace(/'/g, "\\'")}', ${p.price}, ${p.stock})" class="mt-3 w-full ${isOut?'bg-gray-100 text-gray-400':'bg-blue-50 text-blue-700'} py-2.5 rounded-xl font-bold text-sm" ${isOut?'disabled':''}>🛒 ထည့်မည်</button>
                        </div>
                    </div>`
                }).join('');
            }

            function addToCart(id, name, price, maxStock) { 
                let existing = cart.find(i => i.id === id);
                if(existing) { if(existing.qty < maxStock) existing.qty++; else return showToast("လက်ကျန် မလုံလောက်ပါ။"); } 
                else cart.push({id, name, price, qty: 1, maxStock});
                updateCartBadge(); showToast("ခြင်းထဲရောက်ပါပြီ"); 
            }
            
            function updateCartBadge() { 
                const b = document.getElementById('cart-count'); 
                let t = cart.reduce((s, i) => s + i.qty, 0); 
                b.innerText = t; t > 0 ? b.classList.remove('hidden') : b.classList.add('hidden'); 
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
                    <div class="flex justify-between items-center bg-white p-3.5 rounded-2xl border border-gray-100 shadow-sm mb-3">
                        <div class="flex-1 pr-2">
                            <div class="text-sm font-bold text-gray-800 line-clamp-1">${i.name}</div>
                            <div class="text-blue-600 font-bold mt-1 text-[15px]">${(i.price).toLocaleString()} Ks</div>
                        </div>
                        <div class="flex items-center gap-3 bg-gray-50 rounded-xl p-1 border border-gray-200">
                            <button onclick="changeQty(${index}, -1)" class="w-8 h-8 font-bold text-gray-600 bg-white rounded-lg">-</button>
                            <span class="font-bold text-sm min-w-[20px] text-center">${i.qty}</span>
                            <button onclick="changeQty(${index}, 1)" class="w-8 h-8 font-bold text-gray-600 bg-white rounded-lg">+</button>
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

            // ================== CHECKOUT & IMAGE COMPRESSION ==================
            function togglePaymentUI() {
                const isQR = document.getElementById('pay-qr').checked;
                const qrSec = document.getElementById('qr-payment-section');
                if(isQR) qrSec.classList.remove('hidden'); else qrSec.classList.add('hidden');
            }

            function compressImageToBase64(file, callback) {
                const reader = new FileReader();
                reader.onload = function(e) {
                    const img = new Image();
                    img.onload = function() {
                        const canvas = document.createElement('canvas');
                        const MAX_WIDTH = 800;
                        let width = img.width, height = img.height;
                        if (width > MAX_WIDTH) { height = Math.round(height * MAX_WIDTH / width); width = MAX_WIDTH; }
                        canvas.width = width; canvas.height = height;
                        const ctx = canvas.getContext('2d');
                        ctx.drawImage(img, 0, 0, width, height);
                        callback(canvas.toDataURL('image/jpeg', 0.6)); 
                    }
                    img.src = e.target.result;
                }
                reader.readAsDataURL(file);
            }

            async function checkoutCart() {
                const addr = document.getElementById('checkout-address').value.trim();
                const ph = document.getElementById('checkout-phone').value.trim();
                const payMethod = document.querySelector('input[name="pay_method"]:checked').value;
                const tx_id = document.getElementById('checkout-tx').value.trim();
                const fileInput = document.getElementById('checkout-receipt');
                
                if(!addr || !ph) return showToast("လိပ်စာနှင့် ဖုန်းနံပါတ် ဖြည့်ပါ။");
                if(payMethod === "QR" && !tx_id) return showToast("ငွေလွှဲပြေစာ အမှတ် (Tx ID) ထည့်ပါ။");
                
                let base64Receipt = "";
                const finishCheckout = async (b64) => {
                    tg.MainButton.showProgress();
                    try {
                        const payload = { payment_method: payMethod, transaction_id: tx_id, address: addr, phone: ph, receipt_b64: b64, cart: cart.map(i=>({id:i.id, qty:i.qty})) };
                        const res = await apiFetch(`/api/checkout`, { method: 'POST', body: JSON.stringify(payload) });
                        if(res.ok) { 
                            clearCart(); 
                            document.getElementById('checkout-tx').value = '';
                            if(fileInput) fileInput.value = '';
                            showToast("✅ အော်ဒါတင်ခြင်း အောင်မြင်ပါသည်။"); 
                            showTab('history-tab', 'btn-history'); 
                        } else { const err = await res.json(); showToast(err.detail || "Error Occurred"); }
                    } catch(e) { showToast("ဆက်သွယ်မှု ပြတ်တောက်သွားပါသည်။"); }
                    finally { tg.MainButton.hideProgress(); }
                };

                if (payMethod === "QR" && fileInput.files && fileInput.files[0]) {
                    compressImageToBase64(fileInput.files[0], finishCheckout);
                } else {
                    finishCheckout("");
                }
            }

            function clearCart() { cart = []; updateCartBadge(); renderCart(); }

            // ================== BUYER HISTORY ==================
            const statusNames = { 'pending': 'စစ်ဆေးဆဲ ⏳', 'approved': 'ထုပ်ပိုးဆဲ 📦', 'shipped': 'ပို့ဆောင်လိုက်ပြီ 🚚', 'delivered': 'ရောက်ရှိပါပြီ ✅', 'cancelled': 'ပယ်ဖျက်လိုက်သည် ❌' };
            
            async function loadBuyerOrders() {
                document.getElementById('buyer-order-list').innerHTML = '<div class="text-center text-gray-400 py-10">Loading...</div>';
                const res = await apiFetch('/api/buyer/orders');
                const orders = await res.json();
                if(orders.length === 0) return document.getElementById('buyer-order-list').innerHTML = `<div class="text-center text-gray-400 py-10">အော်ဒါမှတ်တမ်း မရှိသေးပါ။</div>`;
                
                document.getElementById('buyer-order-list').innerHTML = orders.map(o => `
                    <div class="bg-white p-4 rounded-2xl shadow-sm border border-gray-100 relative overflow-hidden">
                        <div class="absolute left-0 top-0 bottom-0 w-1 status-${o.status}"></div>
                        <div class="flex justify-between items-start mb-3 pl-2">
                            <span class="text-[13px] font-bold text-gray-800 pr-2">${o.name} <span class="text-blue-500 bg-blue-50 px-1.5 py-0.5 rounded text-[11px] ml-1">x${o.qty}</span></span>
                            <span class="text-[11px] font-bold px-2.5 py-1 rounded-md status-${o.status} whitespace-nowrap">${statusNames[o.status]}</span>
                        </div>
                        <div class="flex justify-between items-center text-xs pl-2">
                            <span class="text-gray-500">${o.pay === 'COD' ? '🚚 အိမ်ရောက်ငွေချေ' : '💳 Mobile Pay'}</span>
                            <span class="font-black text-gray-800 text-[15px]">${(o.price * o.qty).toLocaleString()} Ks</span>
                        </div>
                        ${o.status === 'pending' ? `<button onclick="cancelOrder(${o.id})" class="mt-3 w-full bg-red-50 text-red-600 hover:bg-red-100 py-2 rounded-lg text-xs font-bold transition-colors">အော်ဒါ ပြန်လည်ပယ်ဖျက်မည်</button>` : ''}
                    </div>`).join('');
            }

            async function cancelOrder(orderId) {
                if(!confirm("ဤအော်ဒါကို ဖျက်သိမ်းမှာ သေချာပါသလား?")) return;
                const res = await apiFetch(`/api/buyer/orders/${orderId}/cancel`, {method: 'POST'});
                if(res.ok) { showToast("အော်ဒါ ဖျက်သိမ်းပြီးပါပြီ။"); loadBuyerOrders(); }
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
                if(orders.length === 0) return document.getElementById('order-list').innerHTML = `<div class="text-center text-gray-400 py-10">ဝင်ထားသော အော်ဒါမရှိပါ။</div>`;

                document.getElementById('order-list').innerHTML = orders.map(o => `
                    <div class="bg-white p-4 rounded-2xl shadow-sm border border-gray-100 mb-3 relative overflow-hidden">
                        <div class="absolute left-0 top-0 bottom-0 w-1 status-${o.status}"></div>
                        <div class="flex justify-between mb-3 pl-2">
                            <div class="text-sm font-bold text-gray-800 pr-2">${o.name} <span class="text-blue-500 bg-blue-50 px-1.5 py-0.5 rounded text-xs ml-1">x${o.qty}</span></div>
                            <div class="text-[10px] uppercase font-bold status-${o.status} px-2 py-1 rounded whitespace-nowrap">${statusNames[o.status].split(' ')[0]}</div>
                        </div>
                        <div class="bg-gray-50 p-3 rounded-xl text-[13px] text-gray-700 mb-3 border border-gray-200 ml-2">
                            <div class="mb-1"><span class="font-bold text-gray-500">ဝယ်သူ:</span> <span class="font-medium">${o.buyer}</span></div>
                            <div class="mb-1"><span class="font-bold text-gray-500">လိပ်စာ:</span> <span class="font-medium">${o.addr}</span></div>
                            <div class="mb-1"><span class="font-bold text-gray-500">စနစ်:</span> <span class="font-bold ${o.pay==='COD'?'text-orange-600':'text-blue-600'}">${o.pay}</span></div>
                            ${o.tx ? `<div><span class="font-bold text-gray-500">Tx ID:</span> <span class="text-blue-600 font-mono font-bold bg-blue-50 px-1 rounded">${o.tx}</span></div>` : ''}
                        </div>
                        <div class="flex gap-2 pl-2">
                            <select onchange="updateOrderStatus(${o.id}, this.value)" class="flex-1 bg-gray-50 border border-gray-200 text-[13px] font-bold p-2.5 rounded-xl outline-none focus:ring-2 focus:ring-blue-500">
                                <option value="pending" ${o.status==='pending'?'selected':''}>⏳ စစ်ဆေးဆဲ</option>
                                <option value="approved" ${o.status==='approved'?'selected':''}>📦 အတည်ပြု (ထုပ်ပိုးမည်)</option>
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
                const res = await apiFetch('/api/vendor/products');
                const prods = await res.json();
                document.getElementById('vendor-product-list').innerHTML = prods.map(p => `
                    <div class="bg-white p-4 rounded-2xl shadow-sm border border-gray-100 flex justify-between items-center mb-3">
                        <div>
                            <div class="text-[14px] font-bold text-gray-800 leading-tight mb-1">${p.name}</div>
                            <div class="text-blue-600 font-bold text-[13px]">${p.price.toLocaleString()} Ks (လက်ကျန်: ${p.stock})</div>
                        </div>
                        <button onclick="if(confirm('ဖျက်မှာသေချာပါသလား?')) apiFetch('/api/vendor/products/${p.id}', {method:'DELETE'}).then(loadVendorProducts)" class="text-red-500 text-xs font-bold bg-red-50 px-3 py-1.5 rounded-lg">ဖျက်မည်</button>
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
