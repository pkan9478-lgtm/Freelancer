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
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime, Boolean, Text
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
app = FastAPI(title="Digital Mall Auto-Run System Pro (P2P + Tracker)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_headers=["*"], allow_methods=["*"])

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
DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR}/mall_ai_pro_v2.db")

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
    
    # Vendor Payment Profile Settings
    accept_cod = Column(Boolean, default=False)
    kpay_phone = Column(String, default="")
    wave_phone = Column(String, default="")
    kpay_qr = Column(Text, default="") 
    wave_qr = Column(Text, default="") 

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
    payment_method = Column(String, default="QR") 
    transaction_id = Column(String, default="") 
    address = Column(String) 
    status = Column(String, default="pending") 
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    product = relationship("Product")
    user = relationship("User")

# 📌 Feature: User Notification System
class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    message = Column(String)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

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
# ၄။ API ENDPOINTS (Includes Notifications)
# ==========================================
@app.get("/api/auth")
def authenticate_user(user: User = Depends(get_current_user)):
    vendor_ready = user.accept_cod or user.kpay_phone or user.wave_phone or user.kpay_qr or user.wave_qr
    return {
        "user": {
            "id": user.telegram_id, "name": user.full_name, "role": user.role, 
            "default_address": user.default_address, "phone": user.phone,
            "vendor_ready": bool(vendor_ready),
            "kpay_phone": user.kpay_phone, "wave_phone": user.wave_phone,
            "accept_cod": user.accept_cod
        }
    }

# 📌 5-TIER LOCATION API 
@app.get("/api/locations")
def get_locations():
    file_path = os.path.join(DATA_DIR, "locations.json")
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f: return json.load(f)
    else:
        sample_data = {
            "ရန်ကုန်တိုင်းဒေသကြီး": { "ရန်ကုန်အနောက်ပိုင်းခရိုင်": { "ကမာရွတ်မြို့နယ်": { "ကမာရွတ်(မြို့ပေါ်)": ["အမှတ်(၁) ရပ်ကွက်", "အမှတ်(၂) ရပ်ကွက်"] } } },
            "မန္တလေးတိုင်းဒေသကြီး": { "မန္တလေးခရိုင်": { "ချမ်းအေးသာစံမြို့နယ်": { "ချမ်းအေးသာစံ(မြို့ပေါ်)": ["မြို့မ", "ပတ်ကုန်း"] } } }
        }
        with open(file_path, "w", encoding="utf-8") as f: json.dump(sample_data, f, ensure_ascii=False, indent=4)
        return sample_data

# 📌 Notifications API
@app.get("/api/notifications")
def get_notifications(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    notis = db.query(Notification).filter(Notification.user_id == user.id).order_by(Notification.created_at.desc()).limit(20).all()
    return [{"id": n.id, "message": n.message, "is_read": n.is_read, "date": n.created_at.strftime("%d-%m-%Y %H:%M")} for n in notis]

@app.post("/api/notifications/read")
def mark_notifications_read(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.query(Notification).filter(Notification.user_id == user.id, Notification.is_read == False).update({"is_read": True})
    db.commit()
    return {"status": "success"}

@app.post("/api/vendor/profile")
async def update_vendor_profile(req: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await req.json()
    if user.role == "buyer": user.role = "vendor"
    user.accept_cod = data.get("accept_cod", user.accept_cod)
    user.kpay_phone = data.get("kpay_phone", user.kpay_phone)
    user.wave_phone = data.get("wave_phone", user.wave_phone)
    if "kpay_qr" in data: user.kpay_qr = data.get("kpay_qr")
    if "wave_qr" in data: user.wave_qr = data.get("wave_qr")
    db.commit()
    return {"status": "success"}

@app.get("/api/products")
def get_products(category: str = "All", search: str = "", skip: int = 0, limit: int = 20, db: Session = Depends(get_db)):
    query = db.query(Product)
    if category != "All": query = query.filter(Product.category == category)
    if search: query = query.filter(Product.name.ilike(f"%{search}%"))
    products = query.order_by(Product.id.desc()).offset(skip).limit(limit).all()
    categories = [c[0] for c in db.query(Product.category).distinct().all()] 
    
    res = [{
        "id": p.id, "name": p.name, "price": p.price, "desc": p.description, "category": p.category, 
        "img": p.image_file_id, "stock": p.stock, 
        "vendor_id": p.vendor_id, "vendor_name": p.vendor.full_name,
        "vendor_cod": p.vendor.accept_cod, "vendor_kpay": p.vendor.kpay_phone, 
        "vendor_wave": p.vendor.wave_phone, "kpay_qr": p.vendor.kpay_qr, "wave_qr": p.vendor.wave_qr
    } for p in products]
    
    return {"products": res, "categories": categories}

@app.post("/api/checkout")
async def checkout_cart(req: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await req.json()
    cart_items = data.get('cart', []) 
    tx_id = data.get('transaction_id', '')
    payment_method = data.get('payment_method', 'QR') 
    address = data.get('address', 'Unknown')
    phone = data.get('phone', '')

    if not cart_items: raise HTTPException(status_code=400, detail="Cart is empty")
    total_amount, ordered_names = 0, []
    vendor_notify = None

    for item in cart_items:
        p_id = item.get('id')
        qty = item.get('qty', 1)
        product = db.query(Product).filter(Product.id == p_id).with_for_update().first()
        
        if product and product.stock >= qty:
            db.add(Order(user_id=user.id, product_id=product.id, quantity=qty, transaction_id=tx_id, address=address, payment_method=payment_method))
            product.stock -= qty 
            total_amount += (product.price * qty)
            ordered_names.append(f"{product.name} (x{qty})")
            if product.vendor: vendor_notify = product.vendor.telegram_id
        else:
            db.rollback()
            raise HTTPException(status_code=400, detail=f"'{product.name if product else 'Item'}' ပစ္စည်းလက်ကျန်မလုံလောက်ပါ။")
            
    if address and user.default_address != address: user.default_address = address
    if phone and user.phone != phone: user.phone = phone
    db.commit()

    try:
        items_str = "\n".join([f"- {n}" for n in ordered_names])
        pay_msg = "အိမ်ရောက်မှ ငွေချေစနစ် (COD)" if payment_method == "COD" else f"ငွေလွှဲပြေစာ: `{tx_id}`"
        bot.send_message(user.telegram_id, f"🛒 **အော်ဒါ လက်ခံရရှိပါသည်**\n\n{items_str}\n\nစုစုပေါင်း: {total_amount:,.0f} Ks\nလိပ်စာ: {address}\nငွေချေစနစ်: {pay_msg}\n\n_ရောင်းချသူမှ အတည်ပြုပြီးပါက ဆက်လက်အကြောင်းကြားပေးပါမည်။_", parse_mode="Markdown")
        if vendor_notify:
            bot.send_message(vendor_notify, f"🔔 **အော်ဒါအသစ်ဝင်ပါသည်**\nဝယ်သူ: {user.full_name} (Ph: {phone})\n{items_str}\nလိပ်စာ: {address}\nငွေချေစနစ်: {pay_msg}\n\nApp ထဲတွင် အော်ဒါကို စစ်ဆေး၍ အတည်ပြုပေးပါ။", parse_mode="Markdown")
    except: pass
    return {"status": "success"}

@app.get("/api/buyer/orders")
def get_buyer_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    orders = db.query(Order).filter(Order.user_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "qty": o.quantity, "price": o.product.price, "status": o.status, "date": o.created_at.strftime("%Y-%m-%d"), "pay": o.payment_method} for o in orders]

@app.get("/api/vendor/orders")
def get_vendor_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    orders = db.query(Order).join(Product).filter(Product.vendor_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "qty": o.quantity, "buyer": o.user.full_name, "tx": o.transaction_id, "addr": o.address, "status": o.status, "pay": o.payment_method} for o in orders]

# 📌 Feature: Status Update & Buyer Notification Trigger
@app.post("/api/vendor/orders/{order_id}/status")
def update_order_status(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    status_map = {
        "approved": ("✅ အော်ဒါ အတည်ပြုပါသည်။ ထုပ်ပိုးနေပါသည်။", "ထုပ်ပိုးနေသည်"), 
        "shipped": ("🚚 ပစ္စည်းပို့ဆောင်ပေးလိုက်ပါပြီ။", "ပို့ဆောင်နေသည်"), 
        "delivered": ("🎁 ပစ္စည်းလက်ခံရရှိကြောင်း မှတ်တမ်းတင်ပြီးပါပြီ။", "ရောက်ရှိပါပြီ"), 
        "cancelled": ("❌ အော်ဒါအား ပယ်ဖျက်လိုက်ပါသည်။", "ပယ်ဖျက်လိုက်သည်")
    }
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or (order.product.vendor_id != user.id and user.role != "admin"): raise HTTPException(status_code=400)
    
    new_status = request.query_params.get("status")
    if new_status not in status_map: raise HTTPException(status_code=400)
    if new_status == "cancelled" and order.status != "cancelled": order.product.stock += order.quantity 
    order.status = new_status
    
    # Create In-App Notification for Buyer
    short_status = status_map[new_status][1]
    noti_msg = f"သင့်အော်ဒါ '{order.product.name}' ၏ အခြေအနေမှာ '{short_status}' သို့ ပြောင်းလဲသွားပါသည်။"
    db.add(Notification(user_id=order.user_id, message=noti_msg))
    db.commit()

    # Send Telegram Alert
    try: bot.send_message(order.user.telegram_id, f"{status_map[new_status][0]}\nပစ္စည်း: **{order.product.name} (x{order.quantity})**", parse_mode="Markdown")
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
# ၅။ FRONTEND UI (P2P + Real-time Order Tracker)
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
        <title>Digital Mall P2P & Tracker</title>
        <style>
            body { font-family: sans-serif; -webkit-tap-highlight-color: transparent; background-color: #f3f4f6; }
            .tab-btn.active { color: #2563eb; border-bottom: 3px solid #2563eb; }
            .cat-chip.active { background-color: #2563eb; color: white; border-color: #2563eb; }
            .badge { position: absolute; top: -2px; right: -2px; background: #ef4444; color: white; border-radius: 50%; padding: 2px 6px; font-size: 10px; font-weight: bold; }
            
            /* Modal / Pop-ups */
            .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.5); backdrop-filter: blur(4px); z-index: 60; display: none; align-items: center; justify-content: center; padding: 20px; }
            .modal-overlay.active { display: flex; animation: fadeIn 0.2s ease-out; }
            .slide-up-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 70; display: none; flex-direction: column; justify-content: flex-end; }
            .slide-up-modal.active { display: flex; animation: fadeIn 0.2s; }
            .slide-up-content { background: white; border-radius: 20px 20px 0 0; padding: 20px; max-height: 80vh; overflow-y: auto; animation: slideUp 0.3s ease-out; }
            @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
            @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }
            
            #toast { visibility: hidden; min-width: 250px; background-color: rgba(31, 41, 55, 0.95); color: #fff; text-align: center; border-radius: 12px; padding: 14px; position: fixed; z-index: 100; left: 50%; bottom: 80px; transform: translateX(-50%); font-size: 14px; backdrop-filter: blur(4px); }
            #toast.show { visibility: visible; animation: fadein 0.3s, fadeout 0.3s 2.5s; }
            
            /* Order Tracking Progress Bar */
            .tracker-container { display: flex; justify-content: space-between; align-items: center; position: relative; margin: 15px 10px 5px 10px; }
            .tracker-line { position: absolute; top: 12px; left: 0; right: 0; height: 3px; background-color: #e5e7eb; z-index: 1; }
            .tracker-progress { position: absolute; top: 12px; left: 0; height: 3px; background-color: #2563eb; z-index: 2; transition: width 0.4s ease; }
            .track-step { position: relative; z-index: 3; display: flex; flex-direction: column; align-items: center; gap: 4px; }
            .track-dot { width: 26px; height: 26px; border-radius: 50%; background-color: white; border: 3px solid #e5e7eb; display: flex; align-items: center; justify-content: center; font-size: 12px; color: white; transition: all 0.3s; }
            .track-step.active .track-dot { border-color: #2563eb; background-color: #2563eb; }
            .track-label { font-size: 10px; font-weight: bold; color: #6b7280; }
            .track-step.active .track-label { color: #2563eb; }

            .status-cancelled { background-color: #fee2e2; color: #dc2626; padding: 4px 8px; border-radius: 6px; font-size: 12px; font-weight: bold; }
        </style>
    </head>
    <body class="pb-20">
        
        <header class="bg-white p-4 shadow-sm sticky top-0 z-40 flex justify-between items-center">
            <span class="font-bold text-blue-700 text-xl tracking-tight">Digital<span class="text-gray-800">Mall</span></span>
            <div class="flex items-center gap-3">
                <button onclick="openNotiModal()" class="relative p-2 rounded-full bg-gray-100 text-gray-600 transition">
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" /></svg>
                    <span id="noti-count" class="badge hidden">0</span>
                </button>
                <button onclick="showTab('cart-tab', 'btn-shop')" class="relative p-2.5 rounded-full bg-blue-50 text-blue-600 transition">
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 3h2l.4 2M7 13h10l4-8H5.4M7 13L5.4 5M7 13l-2.293 2.293c-.63.63-.184 1.707.707 1.707H17m0 0a2 2 0 100 4 2 2 0 000-4zm-8 2a2 2 0 11-4 0 2 2 0 014 0z" /></svg>
                    <span id="cart-count" class="badge hidden">0</span>
                </button>
            </div>
        </header>

        <div class="fixed bottom-0 w-full bg-white border-t flex justify-around text-xs font-medium text-gray-500 z-50 shadow-[0_-5px_10px_rgba(0,0,0,0.05)]">
            <button id="btn-shop" onclick="showTab('shop-tab', 'btn-shop')" class="tab-btn active flex-1 py-3 flex flex-col items-center gap-1">
                <span class="text-[20px]">🏠</span><span>ဝယ်မည်</span>
            </button>
            <button onclick="triggerSell()" class="tab-btn flex-1 py-3 flex flex-col items-center gap-1 relative">
                <div class="absolute -top-3 bg-blue-600 text-white w-12 h-12 rounded-full flex items-center justify-center shadow-lg border-4 border-white text-2xl pb-1">+</div>
                <span class="mt-6 font-bold text-blue-600">ရောင်းမည်</span>
            </button>
            <button id="btn-history" onclick="showTab('history-tab', 'btn-history')" class="tab-btn flex-1 py-3 flex flex-col items-center gap-1">
                <span class="text-[20px]">📋</span><span>မှတ်တမ်း</span>
            </button>
            <button id="btn-orders" onclick="showTab('orders-tab', 'btn-orders')" class="tab-btn hidden flex-1 py-3 flex flex-col items-center gap-1">
                <span class="text-[20px]">⚙️</span><span>စီမံရန်</span>
            </button>
        </div>

        <div id="noti-modal" class="slide-up-modal" onclick="closeNotiModal(event)">
            <div class="slide-up-content" onclick="event.stopPropagation()">
                <div class="flex justify-between items-center mb-4">
                    <h2 class="font-bold text-xl text-gray-800">🔔 အသိပေးချက်များ</h2>
                    <button onclick="document.getElementById('noti-modal').classList.remove('active')" class="text-gray-400 font-bold text-2xl">&times;</button>
                </div>
                <div id="noti-list" class="space-y-3 pb-5 min-h-[200px]">
                    <div class="text-center text-gray-400 py-10">အသိပေးချက် မရှိသေးပါ</div>
                </div>
            </div>
        </div>

        <div id="shop-tab" class="tab-content">
            <div class="p-4 bg-white shadow-sm mb-2 rounded-b-2xl">
                <input type="text" id="search-box" oninput="autoSearch()" placeholder="🔍 ရှာဖွေလိုသော ပစ္စည်းအမည်..." class="w-full p-3 bg-gray-50 rounded-xl border border-gray-200 text-sm focus:ring-2 focus:ring-blue-500 outline-none mb-4">
                <div id="category-container" class="flex gap-2 overflow-x-auto pb-1 scrollbar-hide"></div>
            </div>
            <div id="product-list" class="p-4 grid grid-cols-2 gap-4"></div>
        </div>

        <div id="cart-tab" class="tab-content hidden p-4">
            <h2 class="font-bold text-gray-800 text-xl mb-4">🛒 သင်၏ခြင်းတောင်း</h2>
            <div id="cart-empty-state" class="hidden bg-white p-10 rounded-2xl shadow-sm border text-center text-gray-400"><div class="text-4xl mb-2">🛍️</div><p>ပစ္စည်းမရှိပါ</p></div>
            <div id="cart-content-wrapper"></div>
        </div>

        <div id="history-tab" class="tab-content hidden p-4">
            <h2 class="font-bold text-gray-800 text-xl mb-4">အော်ဒါမှတ်တမ်းများ</h2>
            <div id="buyer-order-list" class="space-y-4"></div>
        </div>
        
        <div id="orders-tab" class="tab-content hidden p-4">
            <div class="flex bg-gray-200 p-1 rounded-xl mb-4">
                <button onclick="switchVendorTab('dash')" id="v-tab-dash" class="flex-1 bg-white shadow-sm py-2 rounded-lg text-sm font-bold text-gray-800">အော်ဒါများ</button>
                <button onclick="switchVendorTab('prods')" id="v-tab-prods" class="flex-1 py-2 rounded-lg text-sm font-bold text-gray-500">ပစ္စည်းများ</button>
                <button onclick="switchVendorTab('profile')" id="v-tab-profile" class="flex-1 py-2 rounded-lg text-sm font-bold text-gray-500">Profile</button>
            </div>
            <div id="vendor-dash-view"><div id="order-list" class="space-y-3"></div></div>
            <div id="vendor-prods-view" class="hidden"><div id="vendor-product-list" class="space-y-3"></div></div>
            <div id="vendor-profile-view" class="hidden">
                <div class="bg-white p-5 rounded-2xl shadow-sm border border-gray-100">
                    <h3 class="font-bold text-gray-800 mb-4">🏪 ရောင်းသူ Profile သတ်မှတ်ရန်</h3>
                    <label class="flex items-center gap-3 p-3 bg-blue-50 rounded-xl border border-blue-100 mb-4 cursor-pointer">
                        <input type="checkbox" id="prof-cod" class="w-5 h-5 text-blue-600 rounded">
                        <span class="font-bold text-sm text-blue-900">အိမ်ရောက်မှ ငွေချေစနစ် (COD)</span>
                    </label>
                    <div class="space-y-4">
                        <div class="bg-gray-50 p-3 rounded-xl border border-gray-200">
                            <label class="block text-xs font-bold text-blue-800 mb-1">KPay ဖုန်း / QR</label>
                            <input type="text" id="prof-kpay-ph" placeholder="09xxxxxxxxx" class="w-full p-2.5 bg-white rounded border mb-2 text-sm outline-none">
                            <input type="file" accept="image/*" onchange="encodeImage(this, 'prof-kpay-qr')" class="text-xs mb-2">
                            <input type="hidden" id="prof-kpay-qr"><img id="prof-kpay-preview" class="h-20 rounded hidden border shadow-sm">
                        </div>
                        <div class="bg-gray-50 p-3 rounded-xl border border-gray-200">
                            <label class="block text-xs font-bold text-yellow-600 mb-1">WavePay ဖုန်း / QR</label>
                            <input type="text" id="prof-wave-ph" placeholder="09xxxxxxxxx" class="w-full p-2.5 bg-white rounded border mb-2 text-sm outline-none">
                            <input type="file" accept="image/*" onchange="encodeImage(this, 'prof-wave-qr')" class="text-xs mb-2">
                            <input type="hidden" id="prof-wave-qr"><img id="prof-wave-preview" class="h-20 rounded hidden border shadow-sm">
                        </div>
                    </div>
                    <button onclick="saveVendorProfile()" class="w-full mt-5 bg-blue-600 text-white font-bold py-3 rounded-xl shadow-md">သိမ်းဆည်းမည်</button>
                </div>
            </div>
        </div>

        <div id="setup-modal" class="modal-overlay">
            <div class="bg-white w-full max-w-sm rounded-2xl p-5 shadow-2xl relative">
                <button onclick="document.getElementById('setup-modal').classList.remove('active')" class="absolute top-3 right-4 text-gray-400 font-bold text-xl">&times;</button>
                <h2 class="font-bold text-xl mb-2">ဆိုင်ဖွင့်ရန် လိုအပ်ချက်များ</h2>
                <p class="text-sm text-gray-600 mb-4">ပစ္စည်းမတင်မီ သင်လက်ခံမည့် ငွေချေစနစ်များကို အရင်သတ်မှတ်ပါ။</p>
                <button onclick="document.getElementById('setup-modal').classList.remove('active'); showTab('orders-tab', 'btn-orders'); switchVendorTab('profile');" class="w-full bg-blue-600 text-white font-bold py-3 rounded-xl">Setting သို့သွားရန်</button>
            </div>
        </div>

        <div id="toast">Message</div>

        <script>
            const tg = window.Telegram.WebApp;
            const initData = tg.initData; 
            let allProducts = [], currentCategory = 'All', cart = [];
            let searchTimeout = null, mmData = {}; 
            let currentUser = {};

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
                fetchLocationData(); 
                fetchNotifications(); // Auto-fetch notifications on start
                
                try {
                    const res = await apiFetch('/api/auth');
                    const data = await res.json();
                    currentUser = data.user;
                    
                    document.getElementById('prof-cod').checked = currentUser.accept_cod;
                    document.getElementById('prof-kpay-ph').value = currentUser.kpay_phone || '';
                    document.getElementById('prof-wave-ph').value = currentUser.wave_phone || '';

                    if (currentUser.role === 'vendor' || currentUser.role === 'admin') {
                        document.getElementById('btn-orders').classList.remove('hidden');
                    }
                    loadProducts();
                } catch (e) { showToast("Auth Failed"); }
            }

            // 📌 NOTIFICATION LOGIC
            async function fetchNotifications() {
                try {
                    const res = await apiFetch('/api/notifications');
                    const notis = await res.json();
                    const unreadCount = notis.filter(n => !n.is_read).length;
                    
                    const badge = document.getElementById('noti-count');
                    if(unreadCount > 0) { badge.innerText = unreadCount; badge.classList.remove('hidden'); }
                    else { badge.classList.add('hidden'); }

                    const list = document.getElementById('noti-list');
                    if(notis.length === 0) return;
                    list.innerHTML = notis.map(n => `
                        <div class="p-3 rounded-xl border ${n.is_read ? 'bg-gray-50 border-gray-100 text-gray-500' : 'bg-blue-50 border-blue-200 text-blue-900'}">
                            <div class="font-bold text-sm mb-1">${n.message}</div>
                            <div class="text-[10px] ${n.is_read ? 'text-gray-400' : 'text-blue-500'}">${n.date}</div>
                        </div>
                    `).join('');
                } catch(e) {}
            }

            function openNotiModal() {
                if(tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium');
                document.getElementById('noti-modal').classList.add('active');
                apiFetch('/api/notifications/read', { method: 'POST' }).then(() => {
                    document.getElementById('noti-count').classList.add('hidden');
                });
            }
            function closeNotiModal(e) {
                if(e.target === document.getElementById('noti-modal')) {
                    document.getElementById('noti-modal').classList.remove('active');
                    fetchNotifications(); // Refresh list styles
                }
            }

            // 📌 LOCATION & UI TABS
            async function fetchLocationData() { try { const res = await fetch('/api/locations'); mmData = await res.json(); } catch(e) {} }
            function getSelectOptions(dataObj, defaultText) {
                let html = `<option value="">-- ${defaultText} --</option>`;
                if(dataObj) {
                    if(Array.isArray(dataObj)) dataObj.forEach(v => html += `<option value="${v}">${v}</option>`);
                    else for(let k in dataObj) html += `<option value="${k}">${k}</option>`;
                }
                return html;
            }

            function showTab(tabId, btnId) {
                if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
                document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
                document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
                document.getElementById(tabId).classList.remove('hidden');
                if(btnId) document.getElementById(btnId).classList.add('active');
                
                if(tabId === 'history-tab') loadBuyerOrders();
                if(tabId === 'orders-tab') switchVendorTab('dash');
                if(tabId === 'cart-tab') renderGroupedCart();
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }

            // 📌 SHOPPING & CART logic (Compressed for brevity, identical functionality)
            function triggerSell() {
                if(!currentUser.vendor_ready) { document.getElementById('setup-modal').classList.add('active'); } 
                else { tg.showConfirm("Bot Chat ထဲသို့ ပစ္စည်းပုံနှင့် ဈေးနှုန်းရေးပို့ပါ။ အခုပဲ App ကိုပိတ်ပြီး ပို့မလား?", (r) => { if(r) tg.close(); }); }
            }
            function encodeImage(el, targetId) {
                let f = el.files[0]; if(!f) return;
                let r = new FileReader();
                r.onloadend = function() {
                    document.getElementById(targetId).value = r.result;
                    document.getElementById(targetId + '-preview').src = r.result;
                    document.getElementById(targetId + '-preview').classList.remove('hidden');
                }; r.readAsDataURL(f);
            }
            async function saveVendorProfile() {
                tg.MainButton.showProgress();
                const payload = {
                    accept_cod: document.getElementById('prof-cod').checked,
                    kpay_phone: document.getElementById('prof-kpay-ph').value, wave_phone: document.getElementById('prof-wave-ph').value,
                    kpay_qr: document.getElementById('prof-kpay-qr').value, wave_qr: document.getElementById('prof-wave-qr').value
                };
                const res = await apiFetch('/api/vendor/profile', { method: 'POST', body: JSON.stringify(payload) });
                tg.MainButton.hideProgress();
                if(res.ok) { showToast("Profile သိမ်းဆည်းပြီးပါပြီ။"); currentUser.vendor_ready = true; document.getElementById('btn-orders').classList.remove('hidden'); }
            }

            async function loadProducts(query = "") {
                const res = await apiFetch(`/api/products?category=${currentCategory}&search=${query}`);
                const data = await res.json(); allProducts = data.products;
                if(query === "") {
                    let catsHTML = `<button onclick="filterCategory('All')" class="cat-chip ${currentCategory==='All'?'active':''} whitespace-nowrap px-4 py-2 rounded-full border text-sm bg-white">အားလုံး</button>`;
                    data.categories.forEach(c => catsHTML += `<button onclick="filterCategory('${c}')" class="cat-chip ${currentCategory===c?'active':''} whitespace-nowrap px-4 py-2 rounded-full border text-sm bg-white">${c}</button>`);
                    document.getElementById('category-container').innerHTML = catsHTML;
                }
                if(allProducts.length === 0) return document.getElementById('product-list').innerHTML = `<div class="col-span-2 text-center py-10 text-gray-400">ပစ္စည်းမရှိပါ</div>`;
                
                document.getElementById('product-list').innerHTML = allProducts.map(p => {
                    const imgSrc = p.img ? `/api/image/${p.img}` : 'https://via.placeholder.com/300';
                    const isOut = p.stock <= 0;
                    return `<div class="bg-white rounded-2xl shadow-sm border overflow-hidden flex flex-col relative ${isOut ? 'opacity-60' : ''}">
                        ${isOut ? '<div class="absolute top-2 right-2 bg-red-500 text-white text-[10px] font-bold px-2 py-1 rounded z-10">ကုန်နေပါသည်</div>' : ''}
                        <img src="${imgSrc}" class="w-full h-36 object-cover border-b">
                        <div class="p-3 flex-grow flex flex-col justify-between">
                            <div><div class="text-xs text-gray-400 mb-0.5">🏪 ${p.vendor_name}</div><div class="text-[13px] font-bold text-gray-800 line-clamp-2">${p.name}</div><div class="text-blue-600 text-[15px] font-black mt-1">${p.price.toLocaleString()} Ks</div></div>
                            <button onclick='addToCart(${JSON.stringify(p).replace(/'/g, "&#39;")})' class="mt-3 w-full ${isOut?'bg-gray-100 text-gray-400':'bg-blue-50 text-blue-700'} py-2 rounded-xl font-bold text-sm" ${isOut?'disabled':''}>🛒 ထည့်မည်</button>
                        </div></div>`
                }).join('');
            }
            function filterCategory(cat) { currentCategory = cat; loadProducts(); }
            function autoSearch() { clearTimeout(searchTimeout); searchTimeout = setTimeout(() => { loadProducts(document.getElementById('search-box').value); }, 400); }
            function addToCart(prod) { 
                let ex = cart.find(i => i.id === prod.id);
                if(ex) { if(ex.qty < prod.stock) ex.qty++; else return showToast("Stock မလုံလောက်ပါ။"); } else { cart.push({...prod, qty: 1}); }
                updateCartBadge(); showToast("ခြင်းထဲရောက်ပါပြီ"); 
            }
            function updateCartBadge() { const b = document.getElementById('cart-count'); let t = cart.reduce((s, i) => s + i.qty, 0); b.innerText = t; t > 0 ? b.classList.remove('hidden') : b.classList.add('hidden'); }

            function renderGroupedCart() {
                if(cart.length === 0) { document.getElementById('cart-empty-state').classList.remove('hidden'); document.getElementById('cart-content-wrapper').innerHTML = ''; return; }
                document.getElementById('cart-empty-state').classList.add('hidden');
                let vGroups = {};
                cart.forEach((i, idx) => {
                    if(!vGroups[i.vendor_id]) vGroups[i.vendor_id] = { vendor_name: i.vendor_name, vendor_cod: i.vendor_cod, kpay_phone: i.vendor_kpay, kpay_qr: i.kpay_qr, wave_phone: i.vendor_wave, wave_qr: i.wave_qr, items: [], total: 0 };
                    vGroups[i.vendor_id].items.push({...i, cart_idx: idx}); vGroups[i.vendor_id].total += (i.price * i.qty);
                });
                let html = '';
                for(let vid in vGroups) {
                    let g = vGroups[vid];
                    let itemsHtml = g.items.map(i => `<div class="flex justify-between items-center mb-2 bg-gray-50 p-2 rounded border"><div class="flex-1"><div class="text-sm font-bold line-clamp-1">${i.name}</div><div class="text-blue-600 text-xs">${i.price.toLocaleString()} Ks</div></div><div class="flex items-center gap-2 bg-white rounded shadow-sm px-1 py-0.5 border"><button onclick="changeQty(${i.cart_idx}, -1)" class="w-6 h-6 text-gray-600 font-bold">-</button><span class="text-xs font-bold w-4 text-center">${i.qty}</span><button onclick="changeQty(${i.cart_idx}, 1)" class="w-6 h-6 text-gray-600 font-bold">+</button></div></div>`).join('');
                    let payHtml = `<div class="mt-4 border-t pt-3"><select id="pay_method_${vid}" onchange="togglePayMethod(${vid})" class="w-full p-2 bg-white border rounded text-sm font-bold mb-3">`;
                    if(g.vendor_cod) payHtml += `<option value="COD">🏠 အိမ်ရောက်မှ ငွေချေမည်</option>`;
                    if(g.kpay_phone || g.kpay_qr) payHtml += `<option value="KPay">📲 KPay ဖြင့် ငွေလွှဲမည်</option>`;
                    if(g.wave_phone || g.wave_qr) payHtml += `<option value="Wave">📲 WavePay ဖြင့် ငွေလွှဲမည်</option>`;
                    payHtml += `</select><div id="qr_box_${vid}" class="bg-blue-50 p-3 rounded mb-3 border ${(!g.vendor_cod && (g.kpay_phone || g.wave_phone)) ? '' : 'hidden'}"><div class="flex justify-center mb-2"><img id="qr_img_${vid}" src="${g.kpay_qr || g.wave_qr || ''}" class="max-h-32 rounded shadow ${(!g.kpay_qr && !g.wave_qr) ? 'hidden' : ''}"></div><p id="qr_phone_${vid}" class="text-center font-mono font-bold text-lg text-gray-800">${g.kpay_phone || g.wave_phone || ''}</p><input type="text" id="tx_id_${vid}" placeholder="ငွေလွှဲပြေစာ (Tx ID) ၆ လုံး..." class="w-full mt-2 p-2 border rounded text-sm text-center"></div></div>`;
                    html += `<div class="bg-white p-4 rounded-2xl shadow-sm border mb-4"><div class="flex justify-between items-center mb-3"><h3 class="font-bold text-blue-800">🏪 ${g.vendor_name}</h3><span class="text-sm font-black text-blue-600">${g.total.toLocaleString()} Ks</span></div>${itemsHtml}${payHtml}<button onclick="checkoutVendor(${vid})" class="w-full bg-blue-600 text-white font-bold py-3 rounded-xl shadow-md mt-2">အော်ဒါတင်မည်</button></div>`;
                }
                html += `<div class="bg-gray-50 border p-4 rounded-xl mb-5 mt-6"><h3 class="font-bold text-gray-700 mb-3 text-sm">📍 ပို့ဆောင်ရမည့် လိပ်စာ</h3><div class="space-y-3"><select id="sel-state" onchange="updateAddr('sel-district', mmData[this.value])" class="w-full p-2.5 bg-white rounded border text-sm"></select><select id="sel-district" onchange="updateAddr('sel-township', mmData[document.getElementById('sel-state').value][this.value])" class="w-full p-2.5 bg-white rounded border text-sm"><option value="">-- ခရိုင် --</option></select><select id="sel-township" class="w-full p-2.5 bg-white rounded border text-sm"><option value="">-- မြို့နယ် --</option></select><input type="text" id="input-street" placeholder="အိမ်အမှတ်၊ လမ်းအမည်..." class="w-full p-2.5 bg-white rounded border text-sm"><input type="tel" id="input-phone" placeholder="ဆက်သွယ်ရန် ဖုန်းနံပါတ်..." value="${currentUser.phone||''}" class="w-full p-2.5 bg-white rounded border text-sm"></div></div>`;
                document.getElementById('cart-content-wrapper').innerHTML = html;
                document.getElementById('sel-state').innerHTML = getSelectOptions(mmData, "တိုင်းဒေသကြီး");
                for(let vid in vGroups) togglePayMethod(vid, vGroups[vid]);
            }
            function updateAddr(tId, sData) { document.getElementById(tId).innerHTML = getSelectOptions(sData, "ရွေးချယ်ပါ"); }
            function changeQty(idx, d) { if(d > 0 && cart[idx].qty >= cart[idx].stock) return showToast("Stock မလုံလောက်ပါ။"); cart[idx].qty += d; if(cart[idx].qty <= 0) cart.splice(idx, 1); updateCartBadge(); renderGroupedCart(); }
            function togglePayMethod(vid, gData) {
                const m = document.getElementById(`pay_method_${vid}`).value; const box = document.getElementById(`qr_box_${vid}`);
                if(m === "COD") { box.classList.add("hidden"); } else {
                    box.classList.remove("hidden");
                    if(!gData) { let f = cart.find(i => i.vendor_id == vid); gData = {kpay_phone: f.vendor_kpay, wave_phone: f.vendor_wave, kpay_qr: f.kpay_qr, wave_qr: f.wave_qr}; }
                    const img = document.getElementById(`qr_img_${vid}`); const ph = document.getElementById(`qr_phone_${vid}`);
                    if(m === "KPay") { img.src = gData.kpay_qr || ""; img.classList.toggle("hidden", !gData.kpay_qr); ph.innerText = gData.kpay_phone || "ဖုန်းနံပါတ် မရှိပါ"; } 
                    else if(m === "Wave") { img.src = gData.wave_qr || ""; img.classList.toggle("hidden", !gData.wave_qr); ph.innerText = gData.wave_phone || "ဖုန်းနံပါတ် မရှိပါ"; }
                }
            }
            async function checkoutVendor(vid) {
                const st = document.getElementById('sel-state').value, dist = document.getElementById('sel-district').value, tsp = document.getElementById('sel-township').value, str = document.getElementById('input-street').value.trim(), ph = document.getElementById('input-phone').value.trim();
                if(!st || !dist || !tsp || !str || !ph) return showToast("လိပ်စာနှင့် ဖုန်းနံပါတ် ပြည့်စုံစွာ ဖြည့်ပါ။");
                const m = document.getElementById(`pay_method_${vid}`).value; let txId = "";
                if(m !== "COD") { txId = document.getElementById(`tx_id_${vid}`).value.trim(); if(!txId) return showToast("Tx ID ထည့်ပါ။"); }
                tg.MainButton.showProgress();
                try {
                    const res = await apiFetch(`/api/checkout`, { method: 'POST', body: JSON.stringify({ transaction_id: txId, address: `${str}၊ ${tsp}၊ ${dist}၊ ${st}။`, phone: ph, payment_method: m, cart: cart.filter(i => i.vendor_id == vid).map(i=>({id:i.id, qty:i.qty})) }) });
                    if(res.ok) { cart = cart.filter(i => i.vendor_id != vid); updateCartBadge(); showToast("✅ အော်ဒါတင်ခြင်း အောင်မြင်ပါသည်။"); if(cart.length === 0) showTab('history-tab', 'btn-history'); else renderGroupedCart(); } else showToast("Error Occurred");
                } catch(e) {} finally { tg.MainButton.hideProgress(); }
            }

            // 📌 BUYER HISTORY + VISUAL ORDER TRACKER
            async function loadBuyerOrders() {
                const res = await apiFetch('/api/buyer/orders');
                const orders = await res.json();
                
                // Track Levels logic
                const trackIndex = { 'pending': 1, 'approved': 2, 'shipped': 3, 'delivered': 4, 'cancelled': 0 };

                document.getElementById('buyer-order-list').innerHTML = orders.map(o => {
                    let level = trackIndex[o.status];
                    let progressWidth = level === 0 ? 0 : ((level - 1) / 3) * 100;
                    
                    let trackerHtml = '';
                    if (o.status === 'cancelled') {
                        trackerHtml = `<div class="text-center my-3"><span class="status-cancelled">❌ ဤအော်ဒါအား ပယ်ဖျက်လိုက်ပါသည်</span></div>`;
                    } else {
                        trackerHtml = `
                        <div class="tracker-container">
                            <div class="tracker-line"></div>
                            <div class="tracker-progress" style="width: ${progressWidth}%;"></div>
                            <div class="track-step ${level >= 1 ? 'active' : ''}"><div class="track-dot">✓</div><span class="track-label">စစ်ဆေးဆဲ</span></div>
                            <div class="track-step ${level >= 2 ? 'active' : ''}"><div class="track-dot">📦</div><span class="track-label">ထုပ်ပိုးဆဲ</span></div>
                            <div class="track-step ${level >= 3 ? 'active' : ''}"><div class="track-dot">🚚</div><span class="track-label">ပို့နေပါပြီ</span></div>
                            <div class="track-step ${level >= 4 ? 'active' : ''}"><div class="track-dot">🎁</div><span class="track-label">ရောက်ပါပြီ</span></div>
                        </div>`;
                    }

                    return `
                    <div class="bg-white p-4 rounded-2xl shadow-sm border border-gray-100">
                        <div class="flex justify-between items-start mb-2 border-b border-gray-50 pb-2">
                            <span class="text-[14px] font-bold text-gray-800">${o.name} <span class="text-blue-500 bg-blue-50 px-1 rounded text-[11px]">x${o.qty}</span></span>
                            <span class="font-black text-gray-800">${(o.price * o.qty).toLocaleString()} Ks</span>
                        </div>
                        ${trackerHtml}
                        <div class="flex justify-between items-center text-[10px] text-gray-400 mt-2">
                            <span>${o.pay === 'COD' ? '🏠 COD စနစ်' : '💳 QR ဖြင့်ချေထားသည်'}</span>
                            <span>${o.date}</span>
                        </div>
                    </div>`;
                }).join('');
            }
            
            // 📌 VENDOR MANAGEMENT
            function switchVendorTab(tab) {
                ['dash', 'prods', 'profile'].forEach(t => {
                    document.getElementById(`v-tab-${t}`).className = tab === t ? 'flex-1 bg-white shadow-sm py-2 rounded-lg text-sm font-bold text-gray-800' : 'flex-1 py-2 rounded-lg text-sm font-bold text-gray-500';
                    document.getElementById(`vendor-${t}-view`).style.display = tab === t ? 'block' : 'none';
                });
                if(tab === 'dash') loadVendorOrders(); else if(tab === 'prods') loadVendorProducts();
            }

            async function loadVendorOrders() {
                const res = await apiFetch('/api/vendor/orders'); const orders = await res.json();
                document.getElementById('order-list').innerHTML = orders.map(o => `
                    <div class="bg-white p-3 rounded-xl shadow-sm border border-gray-100 mb-2">
                        <div class="flex justify-between mb-2"><div class="text-sm font-bold">${o.name} (x${o.qty})</div><div class="text-[10px] uppercase font-bold px-2 py-1 rounded bg-gray-100 text-gray-600">${o.status}</div></div>
                        <div class="bg-gray-50 p-2 rounded text-xs text-gray-700 mb-2 border"><div>ဝယ်သူ: ${o.buyer}</div><div>စနစ်: ${o.pay === 'COD' ? '🏠 အိမ်ရောက်မှငွေချေ' : '💳 TxID: <b>'+o.tx+'</b>'}</div><div>လိပ်စာ: ${o.addr}</div></div>
                        <select onchange="updateOrderStatus(${o.id}, this.value)" class="w-full bg-blue-50 border border-blue-200 text-blue-800 p-2 rounded text-xs font-bold outline-none">
                            <option value="pending" ${o.status==='pending'?'selected':''}>⏳ စစ်ဆေးဆဲ</option>
                            <option value="approved" ${o.status==='approved'?'selected':''}>📦 အတည်ပြုမည် (ထုပ်ပိုးမည်)</option>
                            <option value="shipped" ${o.status==='shipped'?'selected':''}>🚚 ပို့ဆောင်လိုက်ပြီ</option>
                            <option value="delivered" ${o.status==='delivered'?'selected':''}>✅ ရောက်ရှိပါပြီ</option>
                            <option value="cancelled" ${o.status==='cancelled'?'selected':''}>❌ ပယ်ဖျက်မည်</option>
                        </select>
                    </div>`).join('');
            }
            async function updateOrderStatus(id, st) { await apiFetch(`/api/vendor/orders/${id}/status?status=${st}`, {method:'POST'}); showToast("အခြေအနေ ပြောင်းလဲပြီးပါပြီ"); loadVendorOrders(); }

            async function loadVendorProducts() {
                const res = await apiFetch('/api/vendor/products'); const prods = await res.json();
                document.getElementById('vendor-product-list').innerHTML = prods.map(p => `<div class="bg-white p-3 rounded-xl shadow-sm border flex justify-between items-center mb-2"><div><div class="text-sm font-bold">${p.name}</div><div class="text-blue-600 text-xs font-bold">${p.price.toLocaleString()} Ks | Stock: ${p.stock}</div></div><div class="flex gap-2"><button onclick="updateStock(${p.id}, ${p.stock + 5})" class="bg-gray-100 px-2 py-1 rounded text-xs font-bold">+5</button><button onclick="if(confirm('ဖျက်မှာသေချာပါသလား?')) apiFetch('/api/vendor/products/${p.id}', {method:'DELETE'}).then(loadVendorProducts)" class="text-red-500 bg-red-50 px-2 py-1 rounded text-xs font-bold">ဖျက်မည်</button></div></div>`).join('');
            }
            async function updateStock(id, ns) { await apiFetch(`/api/vendor/products/${id}/stock`, { method: 'PUT', body: JSON.stringify({stock: ns}) }); loadVendorProducts(); }

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
