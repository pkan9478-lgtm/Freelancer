import os
import hmac
import hashlib
import json
import threading
import datetime
import time
import base64
import requests
from urllib.parse import parse_qs
from fastapi import FastAPI, Depends, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime, Boolean, Text, text
from sqlalchemy.orm import sessionmaker, Session, relationship, declarative_base
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
app = FastAPI(title="Digital Mall Master (Immersive Storefront Edition)")
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
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR}/mall_ai_pro_final.db")

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
    
    # Storefront Profile (NEW FEATURES)
    store_name = Column(String, default="")
    store_desc = Column(String, default="လှပသပ်ရပ်သော ကိုယ်ပိုင်အရောင်းပြခန်းလေးပါ")
    store_state = Column(String, default="")
    store_township = Column(String, default="")

    # Vendor Payment Profile
    accept_cod = Column(Boolean, default=True)
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
    payment_slip = Column(Text, default="")
    address = Column(String) 
    status = Column(String, default="pending") 
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    product = relationship("Product")
    user = relationship("User")

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    message = Column(String)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

Base.metadata.create_all(bind=engine)

# AUTO-MIGRATION (Prevent Server Crash for new Storefront columns)
try:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN store_name TEXT DEFAULT ''"))
        conn.execute(text("ALTER TABLE users ADD COLUMN store_desc TEXT DEFAULT 'လှပသပ်ရပ်သော ကိုယ်ပိုင်အရောင်းပြခန်းလေးပါ'"))
        conn.execute(text("ALTER TABLE users ADD COLUMN store_state TEXT DEFAULT ''"))
        conn.execute(text("ALTER TABLE users ADD COLUMN store_township TEXT DEFAULT ''"))
        conn.execute(text("ALTER TABLE orders ADD COLUMN payment_slip TEXT DEFAULT ''"))
except Exception:
    pass 

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
    vendor_ready = bool(user.store_name and user.store_state and (user.accept_cod or user.kpay_phone or user.wave_phone))
    return {
        "user": {
            "id": user.telegram_id, "name": user.full_name, "role": user.role, 
            "default_address": user.default_address, "phone": user.phone,
            "vendor_ready": vendor_ready,
            "store_name": user.store_name, "store_desc": user.store_desc,
            "store_state": user.store_state, "store_township": user.store_township,
            "kpay_phone": user.kpay_phone, "wave_phone": user.wave_phone, "accept_cod": user.accept_cod
        }
    }

@app.get("/api/locations")
def get_locations():
    return {
        "ရန်ကုန်တိုင်းဒေသကြီး": {"ရန်ကုန်အနောက်ပိုင်းခရိုင်": ["ကမာရွတ်", "လှိုင်", "စမ်းချောင်း", "အလုံ", "ကြည့်မြင်တိုင်", "ဒဂုံ", "ဗဟန်း", "ကျောက်တံတား", "ပန်းဘဲတန်း", "လသာ", "လမ်းမတော်"], "ရန်ကုန်အရှေ့ပိုင်းခရိုင်": ["သင်္ဃန်းကျွန်း", "ရန်ကင်း", "တောင်ဥက္ကလာပ", "မြောက်ဥက္ကလာပ", "သာကေတ", "ဒေါပုံ", "တာမွေ", "ပုဇွန်တောင်", "ဗိုလ်တထောင်", "ဒဂုံမြို့သစ်(တောင်ပိုင်း)", "ဒဂုံမြို့သစ်(မြောက်ပိုင်း)"], "ရန်ကုန်မြောက်ပိုင်းခရိုင်": ["အင်းစိန်", "မင်္ဂလာဒုံ", "ရွှေပြည်သာ", "လှိုင်သာယာ"], "ရန်ကုန်တောင်ပိုင်းခရိုင်": ["သန်လျင်", "ကျောက်တန်း", "ဒလ"]},
        "မန္တလေးတိုင်းဒေသကြီး": {"မန္တလေးခရိုင်": ["အောင်မြေသာစံ", "ချမ်းအေးသာစံ", "မဟာအောင်မြေ", "ချမ်းမြသာစည်", "ပြည်ကြီးတံခွန်", "အမရပူရ"], "ပြင်ဦးလွင်ခရိုင်": ["ပြင်ဦးလွင်"]},
        "နေပြည်တော်": {"ဥတ္တရခရိုင်": ["ဥတ္တရသီရိ", "ပုဗ္ဗသီရိ", "ဇေယျာသီရိ", "တပ်ကုန်း"], "ဒက္ခိဏခရိုင်": ["ဒက္ခိဏသီရိ", "ဇမ္ဗူသီရိ", "ပျဉ်းမနား", "လယ်ဝေး"]}
    } # Note: Shortened for code space, UI handles fully dynamic options

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
    
    # Storefront info Update
    user.store_name = data.get("store_name", user.store_name)
    user.store_desc = data.get("store_desc", user.store_desc)
    user.store_state = data.get("store_state", user.store_state)
    user.store_township = data.get("store_township", user.store_township)
    
    # Payment Info Update
    user.accept_cod = data.get("accept_cod", user.accept_cod)
    user.kpay_phone = data.get("kpay_phone", user.kpay_phone)
    user.wave_phone = data.get("wave_phone", user.wave_phone)
    if "kpay_qr" in data: user.kpay_qr = data.get("kpay_qr")
    if "wave_qr" in data: user.wave_qr = data.get("wave_qr")
    
    db.commit()
    return {"status": "success"}

# Added Location/Local Filtering System
@app.get("/api/products")
def get_products(category: str = "All", search: str = "", state: str = "", township: str = "", skip: int = 0, limit: int = 50, db: Session = Depends(get_db)):
    query = db.query(Product).join(User)
    if category != "All": query = query.filter(Product.category == category)
    if search: query = query.filter(Product.name.ilike(f"%{search}%"))
    
    # Apply Local Filter if requested
    if state: query = query.filter(User.store_state == state)
    if township: query = query.filter(User.store_township == township)
    
    products = query.order_by(Product.id.desc()).offset(skip).limit(limit).all()
    categories = [c[0] for c in db.query(Product.category).distinct().all()] 
    
    res = [{
        "id": p.id, "name": p.name, "price": p.price, "desc": p.description, "category": p.category, 
        "img": p.image_file_id, "stock": p.stock, 
        "vendor_id": p.vendor_id, 
        "vendor_name": p.vendor.store_name if p.vendor.store_name else p.vendor.full_name,
        "vendor_desc": p.vendor.store_desc,
        "vendor_state": p.vendor.store_state, "vendor_township": p.vendor.store_township,
        "vendor_cod": p.vendor.accept_cod, "vendor_kpay": p.vendor.kpay_phone, 
        "vendor_wave": p.vendor.wave_phone, "kpay_qr": p.vendor.kpay_qr, "wave_qr": p.vendor.wave_qr
    } for p in products]
    
    return {"products": res, "categories": categories}

@app.post("/api/checkout")
async def checkout_cart(req: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        data = await req.json()
        cart_items = data.get('cart', []) 
        tx_id = data.get('transaction_id', '')
        payment_slip_b64 = data.get('payment_slip', '') 
        payment_method = data.get('payment_method', 'COD') 
        address = data.get('address', 'Unknown')
        phone = data.get('phone', '')

        if not cart_items: raise HTTPException(status_code=400, detail="ခြင်းတောင်းထဲတွင် ပစ္စည်းမရှိပါ။")
        
        total_amount = 0
        ordered_names = []
        vendor_notify = None

        for item in cart_items:
            p_id = item.get('id')
            qty = item.get('qty', 1)
            
            product = db.query(Product).filter(Product.id == p_id).first()
            if not product:
                db.rollback()
                raise HTTPException(status_code=400, detail="အချို့ပစ္စည်းများမှာ စနစ်ထဲတွင် မရှိတော့ပါ။")
            if product.stock < qty:
                db.rollback()
                raise HTTPException(status_code=400, detail=f"'{product.name}' သည် လက်ကျန် ({product.stock}) သာရှိပါတော့သည်။")
                
            db.add(Order(user_id=user.id, product_id=product.id, quantity=qty, transaction_id=tx_id, address=address, payment_method=payment_method, payment_slip=payment_slip_b64 if payment_slip_b64 else ""))
            product.stock -= qty 
            total_amount += (product.price * qty)
            ordered_names.append(f"{product.name} (x{qty})")
            
            if product.vendor: vendor_notify = product.vendor.telegram_id
                
        if address and user.default_address != address: user.default_address = address
        if phone and user.phone != phone: user.phone = phone
        db.commit()

        try:
            items_str = "\n".join([f"- {n}" for n in ordered_names])
            pay_msg = "အိမ်ရောက်မှ ငွေချေစနစ် (COD)" if payment_method == "COD" else f"ငွေလွှဲပြေစာ ID: `{tx_id}`" if tx_id else "ငွေလွှဲပြေစာ ပူးတွဲပါရှိပါသည်"
            bot.send_message(user.telegram_id, f"🛒 **အော်ဒါ လက်ခံရရှိပါသည်**\n\n{items_str}\n\nစုစုပေါင်း: {total_amount:,.0f} Ks\nလိပ်စာ: {address}\nငွေချေစနစ်: {pay_msg}\n\n_ဆိုင်ရှင်မှ အတည်ပြုပြီးပါက ဆက်လက်အကြောင်းကြားပေးပါမည်။_", parse_mode="Markdown")
            
            if vendor_notify:
                vendor_caption = f"🔔 **ဆိုင်သို့ အော်ဒါအသစ်ဝင်ပါသည်**\nဝယ်သူ: {user.full_name} (Ph: {phone})\n{items_str}\nလိပ်စာ: {address}\nငွေချေစနစ်: {pay_msg}\n\nApp ထဲတွင် အချက်အလက်များကို သေချာစစ်ဆေး၍ အတည်ပြုပေးပါ။"
                if payment_slip_b64 and payment_method != "COD":
                    try:
                        img_data = base64.b64decode(payment_slip_b64.split(',')[1] if ',' in payment_slip_b64 else payment_slip_b64)
                        bot.send_photo(vendor_notify, photo=img_data, caption=vendor_caption, parse_mode="Markdown")
                    except Exception: bot.send_message(vendor_notify, vendor_caption, parse_mode="Markdown")
                else: bot.send_message(vendor_notify, vendor_caption, parse_mode="Markdown")
        except Exception: pass

        return {"status": "success"}

    except HTTPException: raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="စနစ်ချို့ယွင်းမှုဖြစ်ပေါ်နေပါသည်။ ခေတ္တစောင့်ပါ။")

@app.get("/api/buyer/orders")
def get_buyer_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    orders = db.query(Order).filter(Order.user_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "qty": o.quantity, "price": o.product.price, "status": o.status, "date": o.created_at.strftime("%Y-%m-%d"), "pay": o.payment_method} for o in orders]

@app.get("/api/vendor/orders")
def get_vendor_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    orders = db.query(Order).join(Product).filter(Product.vendor_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "qty": o.quantity, "buyer": o.user.full_name, "tx": o.transaction_id, "addr": o.address, "status": o.status, "pay": o.payment_method, "slip_img": o.payment_slip if o.payment_slip else ""} for o in orders]

@app.post("/api/vendor/orders/{order_id}/status")
def update_order_status(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    status_map = { "pending": ("⏳ အော်ဒါကို စစ်ဆေးနေဆဲဖြစ်ပါသည်။", "စစ်ဆေးဆဲ"), "approved": ("✅ အော်ဒါ အတည်ပြုပါသည်။ ထုပ်ပိုးနေပါသည်။", "ထုပ်ပိုးနေသည်"), "shipped": ("🚚 ပစ္စည်းပို့ဆောင်ပေးလိုက်ပါပြီ။", "ပို့ဆောင်နေသည်"), "delivered": ("🎁 ပစ္စည်းလက်ခံရရှိကြောင်း မှတ်တမ်းတင်ပြီးပါပြီ။", "ရောက်ရှိပါပြီ"), "cancelled": ("❌ သင့်အော်ဒါအား ပယ်ဖျက်လိုက်ပါသည်။", "ပယ်ဖျက်လိုက်သည်") }
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or (order.product.vendor_id != user.id and user.role != "admin"): raise HTTPException(status_code=400, detail="Permission Denied")
    
    new_status = request.query_params.get("status")
    if new_status not in status_map: raise HTTPException(status_code=400, detail="Invalid Status")
    
    if new_status == "cancelled" and order.status != "cancelled": order.product.stock += order.quantity 
    elif order.status == "cancelled" and new_status != "cancelled": order.product.stock -= order.quantity

    order.status = new_status
    short_status = status_map[new_status][1]
    db.add(Notification(user_id=order.user_id, message=f"သင့်အော်ဒါ '{order.product.name}' ၏ အခြေအနေမှာ '{short_status}' သို့ ပြောင်းလဲသွားပါသည်။"))
    db.commit()

    try: bot.send_message(order.user.telegram_id, f"{status_map[new_status][0]}\nပစ္စည်း: **{order.product.name} (x{order.quantity})**", parse_mode="Markdown")
    except: pass
    return {"status": "success"}

@app.get("/api/vendor/products")
def get_vendor_products(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    products = db.query(Product).filter(Product.vendor_id == user.id).order_by(Product.id.desc()).all()
    return [{"id":p.id, "name":p.name, "price":p.price, "stock":p.stock, "img":p.image_file_id} for p in products]

@app.put("/api/vendor/products/{product_id}")
async def edit_product(product_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await request.json()
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user.id).first()
    if not product: raise HTTPException(status_code=404, detail="ပစ္စည်းမရှိပါ")
    product.name = data.get("name", product.name); product.price = float(data.get("price", product.price)); product.stock = int(data.get("stock", product.stock))
    db.commit()
    return {"status": "success"}

@app.delete("/api/vendor/products/{product_id}")
def delete_product(product_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user.id).first()
    if product: db.delete(product); db.commit()
    return {"status": "success"}

# ==========================================
# ၅။ AI-POWERED CMS CHAT BOT
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🏬 ကုန်တိုက်သို့ဝင်ရန်", web_app=types.WebAppInfo(WEBAPP_URL)))
    msg = """မင်္ဂလာပါရှင်။\n\n🛍️ **ဈေးဝယ်ရန်** အောက်ပါခလုတ်ကို နှိပ်ပါ။\n📦 **ရောင်းချရန်** ပစ္စည်းဓာတ်ပုံနှင့်တကွ အမည်၊ ဈေးနှုန်းတို့ကို ဤ Chat သို့ တိုက်ရိုက်ပေးပို့လိုက်ရုံဖြင့် AI မှ အလိုအလျောက် ရောင်းချပေးမည် ဖြစ်ပါသည်။"""
    bot.send_message(message.chat.id, msg, reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(content_types=['photo'])
def handle_cms_photo(message):
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == str(message.from_user.id)).first()
    if not user: return db.close()

    # LOCATION & PROFILE GUARD
    if not user.store_name or not user.store_state or not (user.accept_cod or user.kpay_phone or user.wave_phone):
        bot.reply_to(message, "⚠️ **ဆိုင်ဖွင့်ရန် အချက်အလက် မပြည့်စုံသေးပါ။**\n\nကျေးဇူးပြု၍ App အတွင်းရှိ 'စီမံရန် -> Profile' တွင် သင့်ဆိုင်အမည်၊ ဒေသလိပ်စာ နှင့် ငွေပေးချေမှုစနစ်များကို အရင်သတ်မှတ်ပေးပါ။", parse_mode="Markdown")
        return db.close()

    if user.role == "buyer":
        user.role = "vendor"
        db.commit()

    try:
        caption = message.caption or "New Product"
        file_id = message.photo[-1].file_id 
        ai_data = {"name": "New Product", "price": 0, "category": "General", "description": caption, "stock": 10}
        
        if GROQ_API_KEY:
            try:
                msg = bot.reply_to(message, "⏳ AI ဖြင့် ပစ္စည်းအချက်အလက် ခွဲခြမ်းစိတ်ဖြာနေပါသည်...")
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
        bot.reply_to(message, f"✅ **ပစ္စည်း အလိုအလျောက် တင်ပြီးပါပြီ။**\n\n📌 {ai_data['name']}\n💰 {ai_data['price']} Ks\n📦 Stock: {ai_data['stock']}", parse_mode="Markdown")
    except Exception: bot.reply_to(message, f"အမှားအယွင်း ဖြစ်ပေါ်ခဲ့ပါသည်။")
    finally: db.close()

# ==========================================
# ၆။ FRONTEND UI (IMMERSIVE STOREFRONT EDITION)
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
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;800&family=Noto+Sans+Myanmar:wght@400;500;600;800&display=swap" rel="stylesheet">
        <title>Digital Mall - Immersive Storefronts</title>
        <style>
            body { font-family: 'Inter', 'Noto Sans Myanmar', sans-serif; -webkit-tap-highlight-color: transparent; background-color: #f8fafc; }
            @keyframes fadeUp { 0% { opacity: 0; transform: translateY(15px); } 100% { opacity: 1; transform: translateY(0); } }
            .animate-fade-up { animation: fadeUp 0.4s ease-out forwards; }
            @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
            .animate-fade-in { animation: fadeIn 0.3s ease-in-out; }
            @keyframes float { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-5px); } }
            .animate-float { animation: float 3s ease-in-out infinite; }
            @keyframes bounceShort { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.2); } }
            .animate-bounce-short { animation: bounceShort 0.3s ease-out; }

            .gradient-text { background: linear-gradient(135deg, #0ea5e9, #6366f1); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .gradient-bg { background: linear-gradient(135deg, #0ea5e9, #6366f1); }
            
            .glass-header { background: rgba(255, 255, 255, 0.85); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border-bottom: 1px solid rgba(226, 232, 240, 0.8); }
            .glass-bottom-nav { background: rgba(255, 255, 255, 0.90); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border-top: 1px solid rgba(226, 232, 240, 0.8); }
            
            .btn-press:active { transform: scale(0.95); transition: transform 0.1s ease; }
            .tab-btn { color: #64748b; transition: all 0.2s ease; }
            .tab-btn.active { color: #0ea5e9; }
            .cat-chip { transition: all 0.2s ease; border: 1px solid #e2e8f0; }
            .cat-chip.active { background: linear-gradient(135deg, #0ea5e9, #6366f1); color: white; border-color: transparent; box-shadow: 0 4px 6px -1px rgba(14, 165, 233, 0.2); }
            .badge { position: absolute; top: -3px; right: -3px; background: #ef4444; color: white; border-radius: 50%; padding: 2px 6px; font-size: 10px; font-weight: 800; box-shadow: 0 2px 4px rgba(239,68,68,0.3); }
            
            .modal-overlay { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.8); backdrop-filter: blur(6px); z-index: 60; display: none; align-items: center; justify-content: center; padding: 20px; }
            .modal-overlay.active { display: flex; animation: fadeIn 0.2s ease-out; }
            .slide-up-modal { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.5); z-index: 70; display: none; flex-direction: column; justify-content: flex-end; }
            .slide-up-modal.active { display: flex; animation: fadeIn 0.2s; }
            .slide-up-content { background: white; border-radius: 24px 24px 0 0; padding: 24px; max-height: 85vh; overflow-y: auto; animation: slideUp 0.3s cubic-bezier(0.16, 1, 0.3, 1); }
            @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }
            
            /* Storefront Specific Styles */
            .store-hero { position: relative; border-radius: 0 0 32px 32px; overflow: hidden; background: linear-gradient(to bottom right, #f1f5f9, #e2e8f0); padding-bottom: 20px;}
            .store-hero::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 120px; background: linear-gradient(135deg, #0ea5e9, #8b5cf6); opacity: 0.85; }
            .store-avatar { width: 80px; height: 80px; border-radius: 24px; border: 4px solid white; background: white; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.1); position: relative; margin: 70px auto 10px auto; display: flex; justify-content: center; align-items: center; font-size: 32px; z-index: 10;}
            
            #toast { visibility: hidden; min-width: 250px; background: rgba(15, 23, 42, 0.95); color: #fff; text-align: center; border-radius: 16px; padding: 14px 20px; position: fixed; z-index: 100; left: 50%; bottom: 85px; transform: translateX(-50%); font-size: 13px; font-weight: 600; box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1); }
            #toast.show { visibility: visible; animation: fadein 0.3s, fadeout 0.3s 3.5s; }
            
            /* Toggle Switch for Local Filter */
            .toggle-checkbox:checked { right: 0; border-color: #0ea5e9; }
            .toggle-checkbox:checked + .toggle-label { background-color: #0ea5e9; }
            .toggle-checkbox { right: 0; z-index: 1; border-color: #e2e8f0; transition: all 0.3s; }
            .toggle-label { width: 44px; height: 24px; background-color: #cbd5e1; border-radius: 9999px; transition: all 0.3s; }
        </style>
    </head>
    <body class="pb-24">
        
        <header class="glass-header p-4 sticky top-0 z-40 flex justify-between items-center">
            <span class="font-extrabold text-xl tracking-tight gradient-text">Digital<span class="text-slate-800">Mall</span></span>
            <div class="flex items-center gap-3">
                <button onclick="openNotiModal()" class="btn-press relative p-2.5 rounded-full bg-slate-100 text-slate-600 hover:bg-slate-200 transition">
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" /></svg>
                    <span id="noti-count" class="badge hidden">0</span>
                </button>
                <button onclick="showTab('cart-tab', 'btn-shop')" class="btn-press relative p-2.5 rounded-full bg-sky-50 text-sky-600 hover:bg-sky-100 transition">
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 3h2l.4 2M7 13h10l4-8H5.4M7 13L5.4 5M7 13l-2.293 2.293c-.63.63-.184 1.707.707 1.707H17m0 0a2 2 0 100 4 2 2 0 000-4zm-8 2a2 2 0 11-4 0 2 2 0 014 0z" /></svg>
                    <span id="cart-count" class="badge hidden">0</span>
                </button>
            </div>
        </header>

        <div class="glass-bottom-nav fixed bottom-0 w-full flex justify-around text-[11px] font-bold z-50 shadow-[0_-8px_20px_rgba(0,0,0,0.04)] pb-safe">
            <button id="btn-shop" onclick="showTab('shop-tab', 'btn-shop')" class="tab-btn btn-press active flex-1 py-3 flex flex-col items-center gap-1.5">
                <span class="text-[22px] leading-none">🏠</span><span>ဝယ်မည်</span>
            </button>
            <button onclick="triggerSell()" class="tab-btn btn-press flex-1 py-3 flex flex-col items-center gap-1 relative">
                <div class="absolute -top-5 gradient-bg text-white w-14 h-14 rounded-full flex items-center justify-center shadow-[0_8px_16px_rgba(14,165,233,0.3)] border-4 border-[#f8fafc] text-3xl pb-1 animate-float z-10">+</div>
                <span class="mt-7 font-extrabold gradient-text">ရောင်းမည်</span>
            </button>
            <button id="btn-history" onclick="showTab('history-tab', 'btn-history')" class="tab-btn btn-press flex-1 py-3 flex flex-col items-center gap-1.5">
                <span class="text-[22px] leading-none">📋</span><span>မှတ်တမ်း</span>
            </button>
            <button id="btn-orders" onclick="showTab('orders-tab', 'btn-orders')" class="tab-btn btn-press hidden flex-1 py-3 flex flex-col items-center gap-1.5">
                <span class="text-[22px] leading-none">⚙️</span><span>စီမံရန်</span>
            </button>
        </div>

        <div id="store-modal" class="fixed inset-0 bg-slate-50 z-50 hidden flex-col overflow-y-auto pb-24 transition-transform duration-300 transform translate-x-full">
            <button onclick="closeStoreModal()" class="absolute top-4 left-4 z-50 bg-white/50 backdrop-blur rounded-full w-10 h-10 flex items-center justify-center text-slate-800 shadow-sm border border-white/20 font-bold text-xl btn-press">←</button>
            
            <div class="store-hero shadow-sm">
                <div class="store-avatar" id="store-view-avatar">🏪</div>
                <h2 id="store-view-name" class="text-center font-extrabold text-2xl text-slate-800 px-4">ဆိုင်အမည်</h2>
                <div class="flex items-center justify-center gap-1.5 mt-1.5 text-sky-600 text-[11px] font-bold tracking-wide">
                    <span>📍</span><span id="store-view-location">ရန်ကုန်တိုင်းဒေသကြီး၊ ကမာရွတ်</span>
                </div>
                <p id="store-view-desc" class="text-center text-[12px] font-medium text-slate-500 mt-3 px-6 leading-relaxed">ဆိုင်အကြောင်း ဖော်ပြချက်</p>
            </div>
            
            <div class="p-4 pt-6">
                <div class="flex justify-between items-center mb-4 px-1">
                    <h3 class="font-extrabold text-slate-800 text-lg">အရောင်းပြခန်း</h3>
                    <span class="text-[10px] bg-slate-200 text-slate-600 px-2 py-1 rounded-md font-bold" id="store-view-count">0 items</span>
                </div>
                <div id="store-product-list" class="grid grid-cols-2 gap-4"></div>
            </div>
        </div>

        <div id="noti-modal" class="slide-up-modal" onclick="closeNotiModal(event)">
            <div class="slide-up-content" onclick="event.stopPropagation()">
                <div class="flex justify-between items-center mb-5">
                    <h2 class="font-extrabold text-xl text-slate-800">🔔 အသိပေးချက်များ</h2>
                    <button onclick="document.getElementById('noti-modal').classList.remove('active')" class="btn-press text-slate-400 bg-slate-100 rounded-full w-8 h-8 flex items-center justify-center font-bold text-xl">&times;</button>
                </div>
                <div id="noti-list" class="space-y-3 pb-5 min-h-[200px]"></div>
            </div>
        </div>

        <div id="slip-viewer-modal" class="modal-overlay" onclick="closeSlipModal()">
            <div class="w-full max-w-sm rounded-[24px] relative animate-fade-up flex flex-col items-center" onclick="event.stopPropagation()">
                <button onclick="closeSlipModal()" class="absolute -top-12 right-0 text-white bg-white/20 backdrop-blur rounded-full w-10 h-10 flex items-center justify-center font-bold text-2xl btn-press">&times;</button>
                <div class="bg-white p-2 rounded-2xl shadow-2xl w-full">
                    <h3 class="font-extrabold text-center text-slate-800 py-3 border-b border-slate-100">💳 ငွေလွှဲပြေစာ</h3>
                    <img id="slip-viewer-img" class="w-full h-auto max-h-[65vh] object-contain rounded-xl mt-2 bg-slate-50">
                </div>
            </div>
        </div>

        <div id="shop-tab" class="tab-content animate-fade-in">
            <div class="p-4 bg-white shadow-sm border-b border-slate-100 mb-3 rounded-b-3xl">
                
                <div class="flex items-center justify-between mb-4 bg-sky-50 border border-sky-100 p-3 rounded-2xl">
                    <div class="flex flex-col">
                        <span class="font-extrabold text-sky-800 text-sm flex items-center gap-1.5">📍 အနီးနားရှိ ပစ္စည်းများ</span>
                        <span id="local-area-name" class="text-[10px] text-sky-600 font-medium">ဒေသရွေးချယ်ရန် ဖွင့်ပါ</span>
                    </div>
                    <div class="relative inline-block w-11 mr-1 align-middle select-none transition duration-200 ease-in">
                        <input type="checkbox" name="toggle" id="local-filter-toggle" onchange="toggleLocalFilter()" class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 appearance-none cursor-pointer mt-0.5 ml-0.5 shadow-sm"/>
                        <label for="local-filter-toggle" class="toggle-label block overflow-hidden h-6 rounded-full cursor-pointer"></label>
                    </div>
                </div>

                <div class="relative mb-4">
                    <span class="absolute left-4 top-3 text-slate-400">🔍</span>
                    <input type="text" id="search-box" oninput="autoSearch()" placeholder="ရှာဖွေလိုသော ပစ္စည်းအမည်..." class="w-full py-3 pl-10 pr-4 bg-slate-50 rounded-2xl border border-slate-200 text-sm focus:ring-2 focus:ring-sky-500 outline-none transition-all font-medium">
                </div>
                <div id="category-container" class="flex gap-2.5 overflow-x-auto pb-2 scrollbar-hide pt-1"></div>
            </div>
            <div id="product-list" class="p-4 grid grid-cols-2 gap-4 pb-12"></div>
        </div>

        <div id="cart-tab" class="tab-content hidden p-4 animate-fade-in">
            <h2 class="font-extrabold text-slate-800 text-2xl mb-5 flex items-center gap-2">🛒 သင်၏ခြင်းတောင်း</h2>
            <div id="cart-empty-state" class="hidden animate-fade-up bg-white p-12 rounded-3xl shadow-sm border border-slate-100 text-center text-slate-400 mt-10">
                <div class="text-6xl mb-4 animate-float">🛍️</div>
                <p class="font-bold text-slate-500">ပစ္စည်းမရှိသေးပါ</p>
            </div>
            <div id="cart-content-wrapper" class="pb-10"></div>
        </div>

        <div id="history-tab" class="tab-content hidden p-4 animate-fade-in">
            <h2 class="font-extrabold text-slate-800 text-2xl mb-5">📋 အော်ဒါမှတ်တမ်းများ</h2>
            <div id="buyer-order-list" class="space-y-4 pb-10"></div>
        </div>
        
        <div id="orders-tab" class="tab-content hidden p-4 animate-fade-in">
            <div class="flex bg-slate-100 p-1.5 rounded-2xl mb-5 shadow-inner">
                <button onclick="switchVendorTab('dash')" id="v-tab-dash" class="btn-press flex-1 bg-white shadow-sm py-2.5 rounded-xl text-sm font-extrabold text-sky-600 transition-all">အော်ဒါများ</button>
                <button onclick="switchVendorTab('prods')" id="v-tab-prods" class="btn-press flex-1 py-2.5 rounded-xl text-sm font-bold text-slate-500 transition-all">ပစ္စည်းများ</button>
                <button onclick="switchVendorTab('profile')" id="v-tab-profile" class="btn-press flex-1 py-2.5 rounded-xl text-sm font-bold text-slate-500 transition-all">ဆိုင်ပြင်ဆင်ရန်</button>
            </div>
            <div id="vendor-dash-view" class="animate-fade-up"><div id="order-list" class="space-y-4 pb-10"></div></div>
            <div id="vendor-prods-view" class="hidden animate-fade-up"><div id="vendor-product-list" class="space-y-3 pb-10"></div></div>
            <div id="vendor-profile-view" class="hidden animate-fade-up">
                <div class="bg-white p-6 rounded-3xl shadow-sm border border-slate-100 pb-10">
                    <h3 class="font-extrabold text-slate-800 text-lg mb-4 flex items-center gap-2">🏪 ဆိုင်လိပ်စာနှင့် ပြခန်းအချက်အလက်</h3>
                    
                    <div class="space-y-4 mb-6 border-b border-slate-100 pb-6">
                        <div>
                            <label class="block text-xs font-bold text-slate-500 mb-1.5">ဆိုင်အမည် (Store Name) <span class="text-red-500">*</span></label>
                            <input type="text" id="prof-store-name" placeholder="ဥပမာ - Beauty Shop" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-sky-500">
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-500 mb-1.5">ဆိုင်အကြောင်း (Description)</label>
                            <textarea id="prof-store-desc" rows="2" placeholder="ဆိုင်အကြောင်း အတိုချုံး..." class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-sky-500"></textarea>
                        </div>
                        <div class="flex gap-3">
                            <div class="flex-1">
                                <label class="block text-xs font-bold text-slate-500 mb-1.5">တိုင်း / ပြည်နယ် <span class="text-red-500">*</span></label>
                                <select id="prof-state" onchange="updateProfileTownships()" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-sky-500"></select>
                            </div>
                            <div class="flex-1">
                                <label class="block text-xs font-bold text-slate-500 mb-1.5">မြို့နယ် <span class="text-red-500">*</span></label>
                                <select id="prof-township" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-sky-500"><option value="">-- ရွေးချယ်ပါ --</option></select>
                            </div>
                        </div>
                    </div>

                    <h3 class="font-extrabold text-slate-800 text-lg mb-4 flex items-center gap-2">💳 ငွေပေးချေမှု လက်ခံမည့်စနစ်များ</h3>
                    <label class="flex items-center gap-3 p-4 bg-sky-50/50 rounded-2xl border border-sky-100 mb-5 cursor-pointer transition hover:bg-sky-50">
                        <input type="checkbox" id="prof-cod" class="w-5 h-5 text-sky-600 rounded border-gray-300 focus:ring-sky-500">
                        <span class="font-bold text-sm text-sky-900">အိမ်ရောက်မှ ငွေချေစနစ် (COD)</span>
                    </label>
                    <div class="space-y-5">
                        <div class="bg-slate-50 p-4 rounded-2xl border border-slate-200">
                            <label class="block text-xs font-extrabold text-sky-700 mb-2">KPay ဖုန်း / QR</label>
                            <input type="text" id="prof-kpay-ph" placeholder="09xxxxxxxxx" class="w-full p-3 bg-white rounded-xl border border-slate-200 mb-3 text-sm outline-none focus:ring-2 focus:ring-sky-500 transition">
                            <input type="file" accept="image/*" onchange="encodeProfileQR(this, 'prof-kpay-qr')" class="text-xs text-slate-500 file:mr-4 file:py-2 file:px-4 file:rounded-full file:border-0 file:font-bold file:bg-sky-50 file:text-sky-700 hover:file:bg-sky-100 transition">
                            <input type="hidden" id="prof-kpay-qr"><img id="prof-kpay-preview" class="h-24 object-cover rounded-xl hidden border border-slate-200 shadow-sm mt-3">
                        </div>
                        <div class="bg-slate-50 p-4 rounded-2xl border border-slate-200">
                            <label class="block text-xs font-extrabold text-amber-600 mb-2">WavePay ဖုန်း / QR</label>
                            <input type="text" id="prof-wave-ph" placeholder="09xxxxxxxxx" class="w-full p-3 bg-white rounded-xl border border-slate-200 mb-3 text-sm outline-none focus:ring-2 focus:ring-amber-500 transition">
                            <input type="file" accept="image/*" onchange="encodeProfileQR(this, 'prof-wave-qr')" class="text-xs text-slate-500 file:mr-4 file:py-2 file:px-4 file:rounded-full file:border-0 file:font-bold file:bg-amber-50 file:text-amber-700 hover:file:bg-amber-100 transition">
                            <input type="hidden" id="prof-wave-qr"><img id="prof-wave-preview" class="h-24 object-cover rounded-xl hidden border border-slate-200 shadow-sm mt-3">
                        </div>
                    </div>
                    <button onclick="saveVendorProfile()" class="btn-press w-full mt-6 gradient-bg text-white font-bold py-3.5 rounded-xl shadow-md text-sm">ဆိုင်ဖွင့်မည် / ပြင်ဆင်မည်</button>
                </div>
            </div>
        </div>

        <div id="setup-modal" class="modal-overlay">
            <div class="bg-white w-full max-w-sm rounded-[24px] p-6 shadow-2xl relative animate-fade-up">
                <button onclick="document.getElementById('setup-modal').classList.remove('active')" class="btn-press absolute top-4 right-4 text-slate-400 bg-slate-100 rounded-full w-8 h-8 flex items-center justify-center font-bold text-xl">&times;</button>
                <div class="text-4xl mb-3">🏪</div>
                <h2 class="font-extrabold text-xl mb-2 text-slate-800">ဆိုင်စတင် ဖွင့်လှစ်ရန်</h2>
                <p class="text-sm text-slate-500 mb-6 font-medium leading-relaxed">ပစ္စည်းမတင်မီ သင်၏ 'ကိုယ်ပိုင်အရောင်းပြခန်း' အတွက် ဆိုင်အမည်၊ ဒေသလိပ်စာ နှင့် ငွေချေစနစ်များကို အရင်သတ်မှတ်ပေးပါ။</p>
                <button onclick="document.getElementById('setup-modal').classList.remove('active'); showTab('orders-tab', 'btn-orders'); switchVendorTab('profile');" class="btn-press w-full gradient-bg text-white font-bold py-3.5 rounded-xl shadow-md text-sm">ဆိုင်အချက်အလက် သွားဖြည့်မည်</button>
            </div>
        </div>
        
        <div id="buyer-local-modal" class="modal-overlay">
            <div class="bg-white w-full max-w-sm rounded-[24px] p-6 shadow-2xl relative animate-fade-up">
                <button onclick="cancelLocalSetup()" class="btn-press absolute top-4 right-4 text-slate-400 bg-slate-100 rounded-full w-8 h-8 flex items-center justify-center font-bold text-xl">&times;</button>
                <div class="text-4xl mb-3 text-center">📍</div>
                <h2 class="font-extrabold text-xl mb-2 text-slate-800 text-center">သင့်ဒေသကို ရွေးချယ်ပါ</h2>
                <p class="text-xs text-slate-500 mb-5 text-center font-medium">အနီးနားရှိ ဆိုင်များကို ရှာဖွေနိုင်ရန် သင်၏ တိုင်း/ပြည်နယ် နှင့် မြို့နယ်ကို ရွေးချယ်ပေးပါ။</p>
                
                <div class="space-y-3 mb-5">
                    <select id="buyer-state" onchange="updateBuyerTownships()" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-sky-500"></select>
                    <select id="buyer-township" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-sky-500"><option value="">-- မြို့နယ် --</option></select>
                </div>
                <button onclick="confirmLocalSetup()" class="btn-press w-full bg-sky-600 text-white font-bold py-3.5 rounded-xl shadow-md text-sm">အတည်ပြုမည်</button>
            </div>
        </div>

        <div id="toast">Message</div>

        <script>
            const tg = window.Telegram.WebApp;
            const initData = tg.initData; 
            let allProducts = [], currentCategory = 'All', cart = [];
            let searchTimeout = null, mmData = {}; 
            let currentUser = {};
            let isLocalFilterActive = false;
            let currentLocalState = "", currentLocalTownship = "";

            function showToast(msg) {
                const t = document.getElementById("toast");
                t.innerText = msg; t.className = "show animate-bounce-short";
                if(tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
                setTimeout(() => { t.className = t.className.replace("show animate-bounce-short", ""); }, 3200);
            }

            async function apiFetch(url, options = {}) { return fetch(url, { ...options, headers: { 'X-Telegram-Init-Data': initData, 'Content-Type': 'application/json', ...options.headers }}); }

            async function initApp() {
                tg.expand(); tg.ready();
                await fetchLocationData(); 
                fetchNotifications();
                
                try {
                    const res = await apiFetch('/api/auth'); const data = await res.json();
                    currentUser = data.user;
                    
                    // Vendor Setup
                    document.getElementById('prof-store-name').value = currentUser.store_name || '';
                    document.getElementById('prof-store-desc').value = currentUser.store_desc || '';
                    document.getElementById('prof-cod').checked = (currentUser.accept_cod !== undefined) ? currentUser.accept_cod : true;
                    document.getElementById('prof-kpay-ph').value = currentUser.kpay_phone || '';
                    document.getElementById('prof-wave-ph').value = currentUser.wave_phone || '';
                    
                    if(currentUser.store_state) {
                        setTimeout(()=>{
                            document.getElementById('prof-state').value = currentUser.store_state;
                            updateProfileTownships();
                            document.getElementById('prof-township').value = currentUser.store_township;
                        }, 500);
                    }

                    if (currentUser.role === 'vendor' || currentUser.role === 'admin') { document.getElementById('btn-orders').classList.remove('hidden'); }
                    loadProducts();
                } catch (e) { showToast("Authentication Failed"); }
            }

            // LOCATION DATA
            async function fetchLocationData() { 
                try { 
                    const res = await fetch('/api/locations'); mmData = await res.json(); 
                    document.getElementById('prof-state').innerHTML = getSelectOptions(mmData, "တိုင်းဒေသကြီး");
                    document.getElementById('buyer-state').innerHTML = getSelectOptions(mmData, "တိုင်းဒေသကြီး");
                } catch(e) {} 
            }
            function getSelectOptions(dataObj, defaultText) {
                let html = `<option value="">-- ${defaultText} --</option>`;
                if(dataObj) { if(Array.isArray(dataObj)) dataObj.forEach(v => html += `<option value="${v}">${v}</option>`); else for(let k in dataObj) html += `<option value="${k}">${k}</option>`; }
                return html;
            }
            function updateProfileTownships() {
                const s = document.getElementById('prof-state').value;
                let tHTML = `<option value="">-- မြို့နယ် --</option>`;
                if(s && mmData[s]) { for(let d in mmData[s]) { mmData[s][d].forEach(t => tHTML += `<option value="${t}">${t}</option>`); } }
                document.getElementById('prof-township').innerHTML = tHTML;
            }
            function updateBuyerTownships() {
                const s = document.getElementById('buyer-state').value;
                let tHTML = `<option value="">-- မြို့နယ် --</option>`;
                if(s && mmData[s]) { for(let d in mmData[s]) { mmData[s][d].forEach(t => tHTML += `<option value="${t}">${t}</option>`); } }
                document.getElementById('buyer-township').innerHTML = tHTML;
            }

            // CORE UI
            function showTab(tabId, btnId) {
                if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
                document.querySelectorAll('.tab-content').forEach(el => { el.classList.add('hidden'); el.classList.remove('animate-fade-in'); });
                document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
                const tab = document.getElementById(tabId);
                tab.classList.remove('hidden'); void tab.offsetWidth; tab.classList.add('animate-fade-in');
                if(btnId) document.getElementById(btnId).classList.add('active');
                
                if(tabId === 'history-tab') loadBuyerOrders(); 
                if(tabId === 'orders-tab') switchVendorTab('dash'); 
                if(tabId === 'cart-tab') renderGroupedCart();
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }

            // NOTIFICATIONS
            async function fetchNotifications() {
                try {
                    const res = await apiFetch('/api/notifications'); const notis = await res.json();
                    const badge = document.getElementById('noti-count');
                    const unread = notis.filter(n => !n.is_read).length;
                    if(unread > 0) { badge.innerText = unread; badge.classList.remove('hidden'); badge.classList.add('animate-bounce-short'); } else { badge.classList.add('hidden'); }
                    const list = document.getElementById('noti-list');
                    if(notis.length === 0) return list.innerHTML = `<div class="text-center text-slate-400 py-10 font-medium">အသိပေးချက် မရှိသေးပါ</div>`;
                    list.innerHTML = notis.map(n => `<div class="p-4 rounded-2xl border transition-all ${n.is_read ? 'bg-slate-50 border-slate-100 text-slate-500' : 'bg-sky-50 border-sky-100 text-sky-900 shadow-sm'}"><div class="font-bold text-[13px] mb-1.5">${n.message}</div><div class="text-[10px] font-bold ${n.is_read ? 'text-slate-400' : 'text-sky-500'}">${n.date}</div></div>`).join('');
                } catch(e) {}
            }
            function openNotiModal() { if(tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium'); document.getElementById('noti-modal').classList.add('active'); apiFetch('/api/notifications/read', { method: 'POST' }).then(() => document.getElementById('noti-count').classList.add('hidden')); }
            function closeNotiModal(e) { if(e.target === document.getElementById('noti-modal')) { document.getElementById('noti-modal').classList.remove('active'); fetchNotifications(); } }

            // SELLER LOGIC
            function triggerSell() { 
                if(!currentUser.vendor_ready) document.getElementById('setup-modal').classList.add('active'); 
                else tg.showConfirm("Bot Chat ထဲသို့ ပစ္စည်းပုံ၊ အမည်၊ ဈေးနှုန်းတို့ကို ရေးပို့ပါ။ အခုပဲ App ကိုပိတ်ပြီး ပို့မလား?", (r) => { if(r) tg.close(); }); 
            }
            
            function encodeProfileQR(el, targetId) {
                let f = el.files[0]; if(!f) return; let r = new FileReader();
                r.onloadend = function() { 
                    document.getElementById(targetId).value = r.result; 
                    let preview = document.getElementById(targetId + '-preview');
                    if(preview) { preview.src = r.result; preview.classList.remove('hidden'); }
                }; r.readAsDataURL(f);
            }

            function compressAndEncodeSlip(el, targetId) {
                let file = el.files[0]; if(!file) return; let reader = new FileReader();
                reader.onloadend = function(e) { 
                    let img = new Image(); img.onload = function() {
                        let canvas = document.createElement('canvas'); let ctx = canvas.getContext('2d');
                        let maxW = 800, maxH = 800, width = img.width, height = img.height;
                        if (width > height) { if (width > maxW) { height *= maxW / width; width = maxW; } } else { if (height > maxH) { width *= maxH / height; height = maxH; } }
                        canvas.width = width; canvas.height = height; ctx.drawImage(img, 0, 0, width, height);
                        let dataUrl = canvas.toDataURL('image/jpeg', 0.7); document.getElementById(targetId).value = dataUrl; 
                        let preview = document.getElementById(targetId + '-preview'); if(preview) { preview.src = dataUrl; preview.classList.remove('hidden'); }
                    }; img.src = e.target.result;
                }; reader.readAsDataURL(file);
            }
            
            async function saveVendorProfile() {
                const sName = document.getElementById('prof-store-name').value.trim();
                const sState = document.getElementById('prof-state').value;
                const sTown = document.getElementById('prof-township').value;
                
                if(!sName || !sState || !sTown) return showToast("⚠️ ဆိုင်အမည်နှင့် လိပ်စာ အပြည့်အစုံ ရွေးချယ်ပါ။");
                
                tg.MainButton.showProgress();
                const payload = { 
                    store_name: sName, store_desc: document.getElementById('prof-store-desc').value.trim(),
                    store_state: sState, store_township: sTown,
                    accept_cod: document.getElementById('prof-cod').checked, kpay_phone: document.getElementById('prof-kpay-ph').value, wave_phone: document.getElementById('prof-wave-ph').value, kpay_qr: document.getElementById('prof-kpay-qr').value, wave_qr: document.getElementById('prof-wave-qr').value 
                };
                const res = await apiFetch('/api/vendor/profile', { method: 'POST', body: JSON.stringify(payload) }); tg.MainButton.hideProgress();
                if(res.ok) { 
                    showToast("✅ ဆိုင်အချက်အလက် သိမ်းဆည်းပြီးပါပြီ။"); 
                    currentUser.vendor_ready = true; currentUser.store_state = sState; currentUser.store_township = sTown;
                    document.getElementById('btn-orders').classList.remove('hidden'); 
                }
            }

            // LOCAL-FIRST FILTER SYSTEM
            function toggleLocalFilter() {
                const cb = document.getElementById('local-filter-toggle');
                if(cb.checked) {
                    if(!currentLocalState || !currentLocalTownship) {
                        cb.checked = false; document.getElementById('buyer-local-modal').classList.add('active');
                    } else {
                        isLocalFilterActive = true; document.getElementById('local-area-name').innerText = currentLocalTownship + "၊ " + currentLocalState; loadProducts();
                    }
                } else {
                    isLocalFilterActive = false; document.getElementById('local-area-name').innerText = "ဒေသရွေးချယ်ရန် ဖွင့်ပါ"; loadProducts();
                }
            }
            function confirmLocalSetup() {
                const s = document.getElementById('buyer-state').value, t = document.getElementById('buyer-township').value;
                if(!s || !t) return showToast("တိုင်း နှင့် မြို့နယ် ကို ရွေးချယ်ပါ။");
                currentLocalState = s; currentLocalTownship = t;
                document.getElementById('buyer-local-modal').classList.remove('active');
                document.getElementById('local-filter-toggle').checked = true; toggleLocalFilter();
            }
            function cancelLocalSetup() { document.getElementById('buyer-local-modal').classList.remove('active'); document.getElementById('local-filter-toggle').checked = false; }

            // IMMERSIVE STOREFRONT LOGIC
            let fullProductDataCache = [];
            function openStoreModal(vid, vName, vDesc, vState, vTown) {
                if(tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
                document.getElementById('store-view-name').innerText = vName;
                document.getElementById('store-view-desc').innerText = vDesc || "လှပသပ်ရပ်သော ကိုယ်ပိုင်အရောင်းပြခန်းလေးပါ";
                document.getElementById('store-view-location').innerText = `${vTown}၊ ${vState}`;
                document.getElementById('store-view-avatar').innerText = vName.charAt(0).toUpperCase();
                
                const sModal = document.getElementById('store-modal');
                sModal.classList.remove('hidden');
                setTimeout(() => { sModal.classList.remove('translate-x-full'); }, 10);
                
                // Filter products for this store
                const storeProds = fullProductDataCache.filter(p => p.vendor_id === vid);
                document.getElementById('store-view-count').innerText = `${storeProds.length} items`;
                document.getElementById('store-product-list').innerHTML = renderProductHTML(storeProds);
            }
            
            function closeStoreModal() {
                const sModal = document.getElementById('store-modal');
                sModal.classList.add('translate-x-full');
                setTimeout(() => { sModal.classList.add('hidden'); }, 300);
            }

            // SHOPPING
            async function loadProducts(query = "") {
                let url = `/api/products?category=${currentCategory}&search=${query}`;
                if(isLocalFilterActive) url += `&state=${encodeURIComponent(currentLocalState)}&township=${encodeURIComponent(currentLocalTownship)}`;
                
                const res = await apiFetch(url); const data = await res.json(); 
                allProducts = data.products; fullProductDataCache = data.products; // Cache for storefront
                
                if(query === "") {
                    let catsHTML = `<button onclick="filterCategory('All')" class="btn-press cat-chip ${currentCategory==='All'?'active':''} px-5 py-2.5 rounded-full text-xs font-bold bg-white whitespace-nowrap shadow-sm text-slate-600">အားလုံး</button>`;
                    data.categories.forEach(c => catsHTML += `<button onclick="filterCategory('${c}')" class="btn-press cat-chip ${currentCategory===c?'active':''} px-5 py-2.5 rounded-full text-xs font-bold bg-white whitespace-nowrap shadow-sm text-slate-600">${c}</button>`);
                    document.getElementById('category-container').innerHTML = catsHTML;
                }
                
                if(allProducts.length === 0) {
                    let msg = isLocalFilterActive ? "ဤဒေသတွင် ဆိုင်များမရှိသေးပါ 🔍" : "ပစ္စည်းရှာမတွေ့ပါ 🔍";
                    document.getElementById('product-list').innerHTML = `<div class="col-span-2 text-center py-12 text-slate-400 font-medium animate-fade-up">${msg}</div>`;
                } else {
                    document.getElementById('product-list').innerHTML = renderProductHTML(allProducts);
                }
            }
            
            function renderProductHTML(products) {
                return products.map((p, index) => {
                    const imgSrc = p.img ? `/api/image/${p.img}` : 'https://via.placeholder.com/300'; const isOut = p.stock <= 0;
                    const animDelay = (index % 10) * 0.05; 
                    
                    return `<div class="animate-fade-up bg-white rounded-[24px] shadow-[0_4px_16px_rgba(0,0,0,0.03)] border border-slate-100 overflow-hidden flex flex-col relative transition-all duration-300 transform hover:-translate-y-1 hover:shadow-xl ${isOut ? 'opacity-60 grayscale-[30%]' : ''}" style="animation-delay: ${animDelay}s">
                        ${isOut ? '<div class="absolute top-2 right-2 bg-red-500/90 backdrop-blur text-white text-[10px] font-black px-2 py-1 rounded-lg z-30 shadow-sm">ကုန်နေပါသည်</div>' : ''}
                        
                        <div class="pt-5 pb-3 px-4 bg-slate-50 border-b border-slate-100/50 flex flex-col items-center justify-center relative overflow-hidden shrink-0">
                            <div class="absolute bottom-0 w-full h-1/3 bg-gradient-to-t from-slate-200/50 to-transparent"></div>
                            <img src="${imgSrc}" class="w-[90px] h-[90px] object-cover rounded-[18px] shadow-sm border-2 border-white relative z-10 transform transition-transform duration-300 hover:scale-105 bg-white">
                        </div>
                        
                        <div class="p-3.5 flex-grow flex flex-col justify-between bg-white z-20">
                            <div class="mb-2">
                                <div onclick="openStoreModal(${p.vendor_id}, '${p.vendor_name}', '${p.vendor_desc}', '${p.vendor_state}', '${p.vendor_township}')" class="text-[9px] font-extrabold text-sky-500 bg-sky-50 inline-block px-1.5 py-0.5 rounded cursor-pointer btn-press hover:bg-sky-100 uppercase tracking-wider line-clamp-1 mb-1.5 border border-sky-100">🏪 ${p.vendor_name}</div>
                                <div class="text-[13px] font-extrabold text-slate-800 line-clamp-1 leading-snug mb-1.5">${p.name}</div>
                                
                                <div class="flex justify-between items-end mb-2">
                                    <div class="text-sky-600 text-[15px] font-black leading-none">${p.price.toLocaleString()} <span class="text-[10px] font-bold">Ks</span></div>
                                    <div class="text-[9px] font-bold ${isOut ? 'text-red-500 bg-red-50' : 'text-emerald-600 bg-emerald-50'} px-1.5 py-0.5 rounded-md shrink-0 border ${isOut ? 'border-red-100' : 'border-emerald-100'}">📦 Stock: ${p.stock}</div>
                                </div>
                            </div>
                            <button onclick='addToCart(${JSON.stringify(p).replace(/'/g, "&#39;")})' class="btn-press mt-1 w-full ${isOut?'bg-slate-100 text-slate-400':'bg-sky-50 text-sky-700 hover:bg-sky-100'} py-2.5 rounded-xl font-bold text-xs transition-colors" ${isOut?'disabled':''}>🛒 ခြင်းထဲထည့်မည်</button>
                        </div>
                    </div>`
                }).join('');
            }

            function filterCategory(cat) { currentCategory = cat; loadProducts(); }
            function autoSearch() { clearTimeout(searchTimeout); searchTimeout = setTimeout(() => { loadProducts(document.getElementById('search-box').value); }, 400); }
            
            function addToCart(prod) { 
                let ex = cart.find(i => i.id === prod.id); 
                if(ex) { if(ex.qty < prod.stock) ex.qty++; else return showToast("Stock မလုံလောက်ပါ။"); } else { cart.push({...prod, qty: 1}); }
                updateCartBadge(); showToast("🛒 ခြင်းထဲရောက်ပါပြီ"); 
            }
            function updateCartBadge() { 
                const b = document.getElementById('cart-count'); let t = cart.reduce((s, i) => s + i.qty, 0); b.innerText = t; 
                if(t > 0) { b.classList.remove('hidden'); b.classList.remove('animate-bounce-short'); void b.offsetWidth; b.classList.add('animate-bounce-short'); } else { b.classList.add('hidden'); }
            }

            // CART & CHECKOUT
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
                    let itemsHtml = g.items.map(i => `
                    <div class="flex justify-between items-center mb-3 bg-white p-3 rounded-2xl shadow-[0_2px_8px_rgba(0,0,0,0.02)] border border-slate-100">
                        <div class="flex-1 pr-3"><div class="text-[13px] font-bold text-slate-800 line-clamp-1">${i.name}</div><div class="text-sky-600 text-[12px] font-black mt-0.5">${i.price.toLocaleString()} Ks</div></div>
                        <div class="flex items-center gap-3 bg-slate-50 rounded-xl shadow-inner px-2 py-1 border border-slate-100"><button onclick="changeQty(${i.cart_idx}, -1)" class="btn-press w-7 h-7 flex items-center justify-center text-slate-500 font-bold bg-white rounded-lg shadow-sm">-</button><span class="text-xs font-black w-3 text-center text-slate-700">${i.qty}</span><button onclick="changeQty(${i.cart_idx}, 1)" class="btn-press w-7 h-7 flex items-center justify-center text-sky-600 font-bold bg-white rounded-lg shadow-sm">+</button></div>
                    </div>`).join('');
                    
                    let payHtml = `<div class="mt-4 border-t border-slate-100 pt-4">
                        <label class="text-xs font-bold text-slate-500 mb-2 block">ငွေချေစနစ်ရွေးချယ်ရန်</label>
                        <select id="pay_method_${vid}" onchange="togglePayMethod(${vid})" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-[13px] font-bold mb-3 focus:ring-2 focus:ring-sky-500 outline-none transition text-slate-700">`;
                    
                    if(g.vendor_cod || (!g.kpay_phone && !g.wave_phone)) payHtml += `<option value="COD" selected>🏠 အိမ်ရောက်မှ ငွေချေမည် (COD)</option>`;
                    if(g.kpay_phone || g.kpay_qr) payHtml += `<option value="KPay">📲 KPay ဖြင့် ငွေလွှဲမည်</option>`;
                    if(g.wave_phone || g.wave_qr) payHtml += `<option value="Wave">📲 WavePay ဖြင့် ငွေလွှဲမည်</option>`;
                    payHtml += `</select>
                    
                    <div id="qr_box_${vid}" class="bg-sky-50/50 p-4 rounded-2xl mb-4 border border-sky-100 hidden animate-fade-in">
                        <div class="flex justify-center mb-3"><img id="qr_img_${vid}" src="" class="max-h-36 rounded-xl shadow-md border border-white hidden"></div>
                        <p id="qr_phone_${vid}" class="text-center font-mono font-black text-xl text-sky-900 tracking-wider bg-white py-2 rounded-xl border border-sky-100 shadow-sm">-</p>
                        <div class="mt-4 bg-white p-3 rounded-xl border border-sky-100 shadow-sm relative overflow-hidden">
                            <div class="absolute top-0 left-0 w-1 h-full bg-sky-500"></div>
                            <label class="block text-[11px] font-extrabold text-sky-700 mb-2 pl-2">📸 ငွေလွှဲပြေစာ (Screenshot) တင်ရန် <span class="text-red-500">*လိုအပ်ပါသည်</span></label>
                            <input type="file" accept="image/*" id="slip_input_${vid}" onchange="compressAndEncodeSlip(this, 'slip_base64_${vid}')" class="text-xs text-slate-500 file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:text-[10px] file:font-bold file:bg-sky-50 file:text-sky-700 hover:file:bg-sky-100 transition w-full outline-none">
                            <input type="hidden" id="slip_base64_${vid}"><img id="slip_base64_${vid}-preview" class="h-24 object-cover rounded-xl hidden border border-slate-200 shadow-sm mt-3 ml-2">
                        </div>
                        <input type="text" id="tx_id_${vid}" placeholder="ငွေလွှဲပြေစာ (Tx ID) ဂဏန်း ၆ လုံး..." class="w-full mt-3 p-3 border border-sky-200 rounded-xl text-sm text-center outline-none focus:ring-2 focus:ring-sky-500 font-bold text-sky-900 placeholder-sky-300">
                    </div></div>`;
                    
                    html += `<div class="animate-fade-up bg-white p-5 rounded-3xl shadow-sm border border-slate-100 mb-5">
                        <div class="flex justify-between items-center mb-4 pb-3 border-b border-slate-50">
                            <h3 class="font-extrabold text-slate-800 flex items-center gap-1.5"><span class="text-lg">🏪</span> ${g.vendor_name}</h3>
                            <span class="text-[15px] font-black gradient-text">${g.total.toLocaleString()} Ks</span>
                        </div>
                        ${itemsHtml}${payHtml}
                        <button onclick="checkoutVendor(${vid})" class="btn-press w-full gradient-bg text-white font-bold py-3.5 rounded-xl shadow-md mt-2 text-sm">အော်ဒါတင်မည်</button>
                    </div>`;
                }
                html += `<div class="animate-fade-up bg-slate-50 border border-slate-200 p-5 rounded-3xl mb-5 mt-8 shadow-inner">
                    <h3 class="font-extrabold text-slate-700 mb-4 text-sm flex items-center gap-2"><span class="text-lg">📍</span> ပို့ဆောင်ရမည့် လိပ်စာ</h3>
                    <div class="space-y-3.5">
                        <select id="sel-state" onchange="updateAddr('sel-district', mmData[this.value])" class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-sky-500 outline-none transition"></select>
                        <select id="sel-district" onchange="updateAddr('sel-township', mmData[document.getElementById('sel-state').value][this.value])" class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-sky-500 outline-none transition"><option value="">-- ခရိုင် --</option></select>
                        <select id="sel-township" class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-sky-500 outline-none transition"><option value="">-- မြို့နယ် --</option></select>
                        <input type="text" id="input-street" placeholder="အိမ်အမှတ်၊ လမ်းအမည်..." class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-sky-500 outline-none transition">
                        <input type="tel" id="input-phone" placeholder="ဆက်သွယ်ရမည့် ဖုန်းနံပါတ်..." value="${currentUser.phone||''}" class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-sky-500 outline-none transition">
                    </div>
                </div>`;
                document.getElementById('cart-content-wrapper').innerHTML = html; 
                document.getElementById('sel-state').innerHTML = getSelectOptions(mmData, "တိုင်းဒေသကြီး");
                for(let vid in vGroups) togglePayMethod(vid, vGroups[vid]);
            }
            function updateAddr(tId, sData) { document.getElementById(tId).innerHTML = getSelectOptions(sData, "ရွေးချယ်ပါ"); }
            function changeQty(idx, d) { if(d > 0 && cart[idx].qty >= cart[idx].stock) return showToast("Stock မလုံလောက်ပါ။"); cart[idx].qty += d; if(cart[idx].qty <= 0) cart.splice(idx, 1); updateCartBadge(); renderGroupedCart(); }
            function togglePayMethod(vid, gData) {
                const sel = document.getElementById(`pay_method_${vid}`); if(!sel) return;
                const m = sel.value; const box = document.getElementById(`qr_box_${vid}`);
                if(m === "COD") { box.classList.add("hidden"); } else {
                    box.classList.remove("hidden"); 
                    if(!gData) { let f = cart.find(i => i.vendor_id == vid); gData = {kpay_phone: f.vendor_kpay, wave_phone: f.vendor_wave, kpay_qr: f.kpay_qr, wave_qr: f.wave_qr}; }
                    const img = document.getElementById(`qr_img_${vid}`), ph = document.getElementById(`qr_phone_${vid}`);
                    if(m === "KPay") { img.src = gData.kpay_qr || ""; img.classList.toggle("hidden", !gData.kpay_qr); ph.innerText = gData.kpay_phone || "-"; } 
                    else if(m === "Wave") { img.src = gData.wave_qr || ""; img.classList.toggle("hidden", !gData.wave_qr); ph.innerText = gData.wave_phone || "-"; }
                }
            }

            async function checkoutVendor(vid) {
                const st = document.getElementById('sel-state').value, dist = document.getElementById('sel-district').value, tsp = document.getElementById('sel-township').value, str = document.getElementById('input-street').value.trim(), ph = document.getElementById('input-phone').value.trim();
                if(!st || !dist || !tsp || !str || !ph) return showToast("လိပ်စာနှင့် ဖုန်းနံပါတ် ပြည့်စုံစွာ ဖြည့်ပါ။");
                const m = document.getElementById(`pay_method_${vid}`).value; let txId = "", slipBase64 = "";
                if(m !== "COD") { 
                    txId = document.getElementById(`tx_id_${vid}`).value.trim(); slipBase64 = document.getElementById(`slip_base64_${vid}`).value;
                    if(!slipBase64) return showToast("⚠️ ငွေလွှဲပြေစာ (Screenshot) ပုံတင်ပေးရန် လိုအပ်ပါသည်။"); 
                }
                tg.MainButton.showProgress();
                try {
                    const res = await apiFetch(`/api/checkout`, { method: 'POST', body: JSON.stringify({ transaction_id: txId, payment_slip: slipBase64, address: `${str}၊ ${tsp}၊ ${dist}၊ ${st}။`, phone: ph, payment_method: m, cart: cart.filter(i => i.vendor_id == vid).map(i=>({id:i.id, qty:i.qty})) }) });
                    if(res.ok) { cart = cart.filter(i => i.vendor_id != vid); updateCartBadge(); showToast("✅ အော်ဒါတင်ခြင်း အောင်မြင်ပါသည်။"); if(cart.length === 0) showTab('history-tab', 'btn-history'); else renderGroupedCart(); } else { let e = "အမှားအယွင်းဖြစ်ပေါ်ခဲ့ပါသည်။"; try { const ed = await res.json(); if(ed.detail) e = ed.detail; } catch(err) {} showToast("⚠️ " + e); }
                } catch(e) { showToast("⚠️ အင်တာနက်ချိတ်ဆက်မှု ပြတ်တောက်သွားပါသည်။"); } finally { tg.MainButton.hideProgress(); }
            }

            // HISTORY & VENDOR DASHBOARD (UNCHANGED CORE LOGIC)
            async function loadBuyerOrders() {
                const res = await apiFetch('/api/buyer/orders'); const orders = await res.json();
                document.getElementById('buyer-order-list').innerHTML = orders.map((o, index) => {
                    return `<div class="animate-fade-up bg-white p-5 rounded-3xl shadow-sm border border-slate-100" style="animation-delay: ${(index % 10) * 0.1}s">
                        <div class="flex justify-between items-start mb-3 border-b border-slate-50 pb-3"><span class="text-[14px] font-extrabold text-slate-800">${o.name} <span class="text-sky-600 bg-sky-50 px-1.5 py-0.5 rounded-md text-[11px] ml-1">x${o.qty}</span></span><span class="font-black text-slate-800 ml-3">${(o.price * o.qty).toLocaleString()} Ks</span></div>
                        <div class="text-[11px] font-extrabold px-3 py-1.5 rounded-lg inline-block ${o.status==='pending'?'bg-amber-100 text-amber-700':o.status==='cancelled'?'bg-red-100 text-red-700':'bg-emerald-100 text-emerald-700'}">${o.status}</div>
                        <div class="flex justify-between items-center text-[10px] font-bold text-slate-400 mt-4 bg-slate-50 p-2 rounded-xl"><span>${o.pay === 'COD' ? '🏠 COD' : '💳 QR'}</span><span>📅 ${o.date}</span></div>
                    </div>`;
                }).join('');
            }
            
            function switchVendorTab(tab) {
                ['dash', 'prods', 'profile'].forEach(t => { 
                    document.getElementById(`v-tab-${t}`).className = tab === t ? 'btn-press flex-1 bg-white shadow-sm py-2.5 rounded-xl text-sm font-extrabold text-sky-600 transition-all' : 'btn-press flex-1 py-2.5 rounded-xl text-sm font-bold text-slate-500 transition-all hover:bg-slate-200/50'; 
                    document.getElementById(`vendor-${t}-view`).style.display = tab === t ? 'block' : 'none'; 
                });
                if(tab === 'dash') loadVendorOrders(); else if(tab === 'prods') loadVendorProducts();
            }
            
            async function loadVendorOrders() {
                const res = await apiFetch('/api/vendor/orders'); const orders = await res.json();
                document.getElementById('order-list').innerHTML = orders.map((o, index) => {
                    let slipBtnHtml = o.slip_img ? `<button onclick="viewSlip(this.getAttribute('data-img'))" data-img="${o.slip_img}" class="bg-emerald-100 text-emerald-700 hover:bg-emerald-200 px-2 py-1 rounded-lg ml-1 text-[10px] font-extrabold border border-emerald-200 transition btn-press shadow-sm">📸 ပြေစာကြည့်မည်</button>` : '';
                    return `<div class="animate-fade-up bg-white p-4 rounded-3xl shadow-sm border border-slate-100 mb-4" style="animation-delay: ${(index % 10) * 0.1}s">
                    <div class="flex justify-between items-start mb-3"><div class="text-sm font-extrabold text-slate-800 pr-2">${o.name} <span class="text-sky-600">x${o.qty}</span></div><div class="text-[10px] uppercase font-black px-2.5 py-1 rounded-lg ${o.status==='pending'?'bg-amber-100 text-amber-700':o.status==='cancelled'?'bg-red-100 text-red-700':'bg-emerald-100 text-emerald-700'}">${o.status}</div></div>
                    <div class="bg-slate-50 p-3 rounded-2xl text-[12px] font-medium text-slate-600 mb-3 border border-slate-100 space-y-1.5 leading-relaxed">
                        <div><span class="text-slate-400">👤</span> ${o.buyer}</div>
                        <div><span class="text-slate-400">💳</span> ${o.pay === 'COD' ? '🏠 COD' : 'TxID: <b>' + (o.tx || '-') + '</b>'} ${slipBtnHtml}</div>
                        <div class="flex items-start gap-1.5"><span class="text-slate-400 mt-0.5">📍</span> <span class="line-clamp-2">${o.addr}</span></div>
                    </div>
                    <select onchange="updateOrderStatus(${o.id}, this.value)" class="w-full bg-white border border-sky-200 text-sky-700 p-3 rounded-xl text-xs font-bold outline-none focus:ring-2 focus:ring-sky-500 shadow-sm transition"><option value="pending" ${o.status==='pending'?'selected':''}>⏳ စစ်ဆေးဆဲ</option><option value="approved" ${o.status==='approved'?'selected':''}>📦 အတည်ပြုမည်</option><option value="shipped" ${o.status==='shipped'?'selected':''}>🚚 ပို့ဆောင်လိုက်ပြီ</option><option value="delivered" ${o.status==='delivered'?'selected':''}>✅ ရောက်ရှိပါပြီ</option><option value="cancelled" ${o.status==='cancelled'?'selected':''}>❌ ပယ်ဖျက်မည်</option></select>
                </div>`}).join('');
            }
            
            function viewSlip(imgData) { document.getElementById('slip-viewer-img').src = imgData; document.getElementById('slip-viewer-modal').classList.add('active'); }
            function closeSlipModal() { document.getElementById('slip-viewer-modal').classList.remove('active'); setTimeout(() => { document.getElementById('slip-viewer-img').src = ''; }, 300); }

            function updateOrderStatus(id, st) {
                tg.showConfirm(`ဤအော်ဒါကို ပြောင်းလဲမှာ သေချာပါသလား?`, async function(c) {
                    if(c) { tg.MainButton.showProgress(); try { await apiFetch(`/api/vendor/orders/${id}/status?status=${st}`, {method:'POST'}); showToast("✅ အခြေအနေ ပြောင်းလဲပြီးပါပြီ"); } catch(e) {} tg.MainButton.hideProgress(); loadVendorOrders(); } else { loadVendorOrders(); }
                });
            }
            
            async function loadVendorProducts() {
                const res = await apiFetch('/api/vendor/products'); const prods = await res.json();
                document.getElementById('vendor-product-list').innerHTML = prods.map((p, index) => {
                    return `<div class="animate-fade-up bg-white p-3.5 rounded-3xl shadow-sm border border-slate-100 flex gap-4 items-center mb-3" style="animation-delay: ${(index % 10) * 0.1}s">
                        <img src="${p.img ? '/api/image/'+p.img : 'https://via.placeholder.com/300'}" class="w-20 h-20 object-cover rounded-2xl shadow-sm border border-slate-100 bg-slate-50">
                        <div class="flex-1"><div class="text-[13px] font-extrabold text-slate-800 line-clamp-1 mb-1">${p.name}</div><div class="text-sky-600 text-sm font-black mb-1.5">${p.price.toLocaleString()} Ks</div><div class="text-[10px] text-slate-500 font-extrabold bg-slate-100 inline-flex px-2 py-0.5 rounded-md border border-slate-200">📦 Stock: ${p.stock}</div></div>
                        <div class="flex flex-col gap-2"><button onclick="if(confirm('ဖျက်မှာ သေချာပါသလား?')) apiFetch('/api/vendor/products/${p.id}', {method:'DELETE'}).then(loadVendorProducts)" class="btn-press text-red-500 bg-red-50 px-4 py-1.5 rounded-xl text-[11px] font-extrabold transition hover:bg-red-100">ဖျက်မည်</button></div>
                    </div>`
                }).join('');
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
