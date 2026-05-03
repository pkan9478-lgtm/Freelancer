import os
import hmac
import hashlib
import json
import threading
import datetime
import time
import requests
from urllib.parse import parse_qs
from fastapi import FastAPI, Depends, HTTPException, Request, Header, WebSocket, WebSocketDisconnect
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
app = FastAPI(title="Digital Mall Auto-Run System Pro (Cover Hidden Features)")
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

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if "sqlite" in DATABASE_URL:
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL, pool_size=10, max_overflow=20)

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
    
    product_type = Column(String, default="physical") 
    digital_file_id = Column(String, default="")

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

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    message = Column(String)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True)
    sender_id = Column(Integer, ForeignKey("users.id"))
    text = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    sender = relationship("User")

Base.metadata.create_all(bind=engine)

# Auto-Migration Hacks
try:
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE products ADD COLUMN product_type VARCHAR DEFAULT 'physical'"))
        conn.execute(text("ALTER TABLE products ADD COLUMN digital_file_id VARCHAR DEFAULT ''"))
        conn.commit()
except: pass

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# ==========================================
# ၃။ SECURE AUTHENTICATION & WEBSOCKET
# ==========================================
def verify_tg_data(tg_data_str: str, db: Session):
    try:
        vals = {k: v[0] for k, v in parse_qs(tg_data_str).items()}
        hash_str = vals.pop('hash', None)
        data_check_str = "\n".join([f"{k}={v}" for k, v in sorted(vals.items())])
        secret_key = hmac.new("WebAppData".encode(), BOT_TOKEN.encode(), hashlib.sha256).digest()
        hmac_res = hmac.new(secret_key, data_check_str.encode(), hashlib.sha256).hexdigest()
        if hmac_res != hash_str: return None
        tg_user = json.loads(vals['user'])
        
        db_user = db.query(User).filter(User.telegram_id == str(tg_user['id'])).first()
        if not db_user:
            role = "admin" if str(tg_user['id']) == ADMIN_TELEGRAM_ID else "buyer"
            db_user = User(telegram_id=str(tg_user['id']), full_name=tg_user.get('first_name', 'User'), role=role)
            db.add(db_user)
            db.commit()
            db.refresh(db_user)
        return db_user
    except: return None

def get_current_user(x_telegram_init_data: str = Header(None), db: Session = Depends(get_db)):
    if not x_telegram_init_data: raise HTTPException(status_code=401)
    user = verify_tg_data(x_telegram_init_data, db)
    if not user: raise HTTPException(status_code=401)
    return user

class ConnectionManager:
    def __init__(self): self.active_connections: list[WebSocket] = []
    async def connect(self, websocket: WebSocket): await websocket.accept(); self.active_connections.append(websocket)
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections: self.active_connections.remove(websocket)
    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try: await connection.send_json(message)
            except: pass

chat_manager = ConnectionManager()

# ==========================================
# ၄။ API ENDPOINTS (REST + WEBSOCKET)
# ==========================================
@app.get("/api/auth")
def authenticate_user(user: User = Depends(get_current_user)):
    vendor_ready = user.accept_cod or user.kpay_phone or user.wave_phone or user.kpay_qr or user.wave_qr
    return { "user": { "id": user.telegram_id, "name": user.full_name, "role": user.role, "default_address": user.default_address, "phone": user.phone, "vendor_ready": bool(vendor_ready), "kpay_phone": user.kpay_phone, "wave_phone": user.wave_phone, "accept_cod": user.accept_cod } }

@app.get("/api/locations")
def get_locations():
    return {"ရန်ကုန်တိုင်းဒေသကြီး": {"ရန်ကုန်အနောက်ပိုင်းခရိုင်": ["ကမာရွတ်", "လှိုင်", "စမ်းချောင်း", "အလုံ"], "ရန်ကုန်အရှေ့ပိုင်းခရိုင်": ["သင်္ဃန်းကျွန်း", "ရန်ကင်း", "တောင်ဥက္ကလာပ"]}, "မန္တလေးတိုင်းဒေသကြီး": {"မန္တလေးခရိုင်": ["အောင်မြေသာစံ", "ချမ်းအေးသာစံ", "မဟာအောင်မြေ"]}}

@app.get("/api/image/{file_id}")
def get_telegram_image(file_id: str):
    try:
        file_info = bot.get_file(file_id)
        res = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}")
        return Response(content=res.content, media_type="image/jpeg")
    except: raise HTTPException(status_code=404)

@app.get("/api/notifications")
def get_notifications(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    notis = db.query(Notification).filter(Notification.user_id == user.id).order_by(Notification.created_at.desc()).limit(20).all()
    return [{"id": n.id, "message": n.message, "is_read": n.is_read, "date": n.created_at.strftime("%d-%m-%Y %H:%M")} for n in notis]

@app.post("/api/notifications/read")
def mark_notifications_read(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.query(Notification).filter(Notification.user_id == user.id, Notification.is_read == False).update({"is_read": True}); db.commit()
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
    # Notice: digital_file_id is explicitly NOT passed to the frontend for complete security!
    res = [{"id": p.id, "name": p.name, "price": p.price, "desc": p.description, "category": p.category, "img": p.image_file_id, "stock": p.stock, "vendor_id": p.vendor_id, "vendor_name": p.vendor.full_name, "vendor_cod": p.vendor.accept_cod, "vendor_kpay": p.vendor.kpay_phone, "vendor_wave": p.vendor.wave_phone, "kpay_qr": p.vendor.kpay_qr, "wave_qr": p.vendor.wave_qr, "type": p.product_type} for p in products]
    return {"products": res, "categories": categories}

@app.post("/api/checkout")
async def checkout_cart(req: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        data = await req.json()
        cart_items = data.get('cart', []); tx_id = data.get('transaction_id', ''); payment_method = data.get('payment_method', 'QR'); address = data.get('address', 'Unknown'); phone = data.get('phone', '')
        if not cart_items: raise HTTPException(status_code=400, detail="ခြင်းတောင်းထဲတွင် ပစ္စည်းမရှိပါ။")
        total_amount = 0; ordered_names = []; vendor_notify = None

        for item in cart_items:
            product = db.query(Product).filter(Product.id == item.get('id')).first()
            if not product: db.rollback(); raise HTTPException(status_code=400, detail="အချို့ပစ္စည်းများမှာ စနစ်ထဲတွင် မရှိတော့ပါ။")
            qty = item.get('qty', 1)
            
            if product.product_type == "physical" and product.stock < qty: 
                db.rollback(); raise HTTPException(status_code=400, detail=f"'{product.name}' သည် လက်ကျန် ({product.stock}) သာရှိပါတော့သည်။")
                
            db.add(Order(user_id=user.id, product_id=product.id, quantity=qty, transaction_id=tx_id, address=address, payment_method=payment_method))
            if product.product_type == "physical": product.stock -= qty
            total_amount += (product.price * qty); ordered_names.append(f"{product.name} (x{qty})")
            if product.vendor: vendor_notify = product.vendor.telegram_id
                
        if address and user.default_address != address: user.default_address = address
        if phone and user.phone != phone: user.phone = phone
        db.commit()

        try:
            items_str = "\n".join([f"- {n}" for n in ordered_names])
            pay_msg = "အိမ်ရောက်မှ ငွေချေစနစ် (COD)" if payment_method == "COD" else f"ငွေလွှဲပြေစာ: `{tx_id}`"
            bot.send_message(user.telegram_id, f"🛒 **အော်ဒါ လက်ခံရရှိပါသည်**\n\n{items_str}\n\nစုစုပေါင်း: {total_amount:,.0f} Ks\nငွေချေစနစ်: {pay_msg}\n\n_ရောင်းသူမှ အတည်ပြုပြီးပါက Digital ဖိုင်များရှိလျှင် ဤ Chat သို့ တိုက်ရိုက် ရောက်ရှိလာပါမည်။_", parse_mode="Markdown")
            if vendor_notify: bot.send_message(vendor_notify, f"🔔 **အော်ဒါအသစ်ဝင်ပါသည်**\nဝယ်သူ: {user.full_name} (Ph: {phone})\n{items_str}\nလိပ်စာ: {address}\nငွေချေစနစ်: {pay_msg}", parse_mode="Markdown")
        except: pass
        return {"status": "success"}
    except HTTPException: raise
    except Exception: db.rollback(); raise HTTPException(status_code=500, detail="စနစ်ချို့ယွင်းမှုဖြစ်ပေါ်နေပါသည်။")

@app.get("/api/buyer/orders")
def get_buyer_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    orders = db.query(Order).filter(Order.user_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "qty": o.quantity, "price": o.product.price, "status": o.status, "date": o.created_at.strftime("%Y-%m-%d"), "pay": o.payment_method} for o in orders]

@app.get("/api/vendor/orders")
def get_vendor_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    orders = db.query(Order).join(Product).filter(Product.vendor_id == user.id).order_by(Order.created_at.desc()).all()
    return [{"id": o.id, "name": o.product.name, "qty": o.quantity, "buyer": o.user.full_name, "tx": o.transaction_id, "addr": o.address, "status": o.status, "pay": o.payment_method} for o in orders]

# 📌 Auto Delivery Logic for Digital Goods
@app.post("/api/vendor/orders/{order_id}/status")
def update_order_status(order_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    status_map = {"approved": ("✅ အော်ဒါ အတည်ပြုပါသည်။", "အတည်ပြုသည်"), "shipped": ("🚚 ပစ္စည်းပို့ဆောင်လိုက်ပါပြီ။", "ပို့ဆောင်နေသည်"), "delivered": ("🎁 ပစ္စည်းရောက်ရှိပါပြီ။", "ရောက်ရှိပါပြီ"), "cancelled": ("❌ ပယ်ဖျက်လိုက်ပါသည်။", "ပယ်ဖျက်လိုက်သည်")}
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or (order.product.vendor_id != user.id and user.role != "admin"): raise HTTPException(status_code=400)
    
    new_status = request.query_params.get("status")
    if new_status not in status_map: raise HTTPException(status_code=400)
    
    if new_status == "cancelled" and order.status != "cancelled" and order.product.product_type == "physical": 
        order.product.stock += order.quantity 
        
    # Send Digital Item securely upon Approval
    if new_status == "approved" and order.status != "approved":
        try:
            if order.product.product_type == "ebook" and order.product.digital_file_id:
                bot.send_document(order.user.telegram_id, order.product.digital_file_id, caption=f"📚 အော်ဒါအတည်ပြုပြီးဖြစ်၍ E-book အား ပေးပို့လိုက်ပါသည်။\n\n📖 {order.product.name}")
            elif order.product.product_type == "audio" and order.product.digital_file_id:
                bot.send_audio(order.user.telegram_id, order.product.digital_file_id, caption=f"🎧 အော်ဒါအတည်ပြုပြီးဖြစ်၍ Audio Book အား ပေးပို့လိုက်ပါသည်။\n\n🎵 {order.product.name}")
        except Exception as e: print("Delivery Error:", e)

    order.status = new_status
    db.add(Notification(user_id=order.user_id, message=f"အော်ဒါ '{order.product.name}' ၏ အခြေအနေမှာ '{status_map[new_status][1]}' သို့ ပြောင်းလဲသွားပါသည်။")); db.commit()
    return {"status": "success"}

@app.get("/api/vendor/products")
def get_vendor_products(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    products = db.query(Product).filter(Product.vendor_id == user.id).order_by(Product.id.desc()).all()
    return [{"id":p.id, "name":p.name, "price":p.price, "stock":p.stock, "img":p.image_file_id, "type":p.product_type} for p in products]

@app.put("/api/vendor/products/{product_id}")
async def edit_product(product_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await request.json()
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user.id).first()
    if not product: raise HTTPException(status_code=404)
    product.name = data.get("name", product.name); product.price = float(data.get("price", product.price)); product.stock = int(data.get("stock", product.stock)); db.commit()
    return {"status": "success"}

@app.delete("/api/vendor/products/{product_id}")
def delete_product(product_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user.id).first()
    if product: db.delete(product); db.commit()
    return {"status": "success"}

@app.get("/api/chat/history")
def get_chat_history(db: Session = Depends(get_db)):
    messages = db.query(ChatMessage).order_by(ChatMessage.created_at.desc()).limit(50).all()
    return [{"id": m.id, "sender_name": m.sender.full_name, "sender_role": m.sender.role, "sender_tg": m.sender.telegram_id, "text": m.text, "time": m.created_at.strftime("%I:%M %p")} for m in reversed(messages)]

@app.websocket("/ws/chat")
async def chat_websocket(websocket: WebSocket, tg_data: str, db: Session = Depends(get_db)):
    user = verify_tg_data(tg_data, db)
    if not user: await websocket.close(); return
    await chat_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if not data.strip(): continue
            new_msg = ChatMessage(sender_id=user.id, text=data.strip())
            db.add(new_msg); db.commit(); db.refresh(new_msg)
            await chat_manager.broadcast({ "id": new_msg.id, "sender_name": user.full_name, "sender_role": user.role, "sender_tg": user.telegram_id, "text": new_msg.text, "time": new_msg.created_at.strftime("%I:%M %p") })
    except WebSocketDisconnect: chat_manager.disconnect(websocket)

# ==========================================
# ၅။ AI-POWERED CMS CHAT BOT (Two-Step Upload)
# ==========================================
pending_digital_uploads = {} # In-memory store for 2-step upload

@bot.message_handler(commands=['start'])
def start(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🏬 ကုန်တိုက်သို့ဝင်ရန်", web_app=types.WebAppInfo(WEBAPP_URL)))
    bot.send_message(message.chat.id, "မင်္ဂလာပါရှင်။\n\n🛍️ **ဈေးဝယ်ရန်** အောက်ပါခလုတ်ကို နှိပ်ပါ။\n📦 **ရောင်းချရန်** ရုပ်ဝတ္ထုအတွက် ဓာတ်ပုံကို တိုက်ရိုက်ပို့ပါ။ \nE-book/Audio ရောင်းမည်ဆိုပါက ဖိုင်ကိုအရင်ပို့ပြီးမှ Cover ပုံကို ဆက်ပို့ပေးပါ။", reply_markup=markup, parse_mode="Markdown")

# 📌 STEP 1: Digital File ကို အရင်လက်ခံမည်
@bot.message_handler(content_types=['document', 'audio'])
def handle_digital_file(message):
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == str(message.from_user.id)).first()
    db.close()
    
    if not user or not (user.accept_cod or user.kpay_phone or user.wave_phone or user.kpay_qr):
        return bot.reply_to(message, "⚠️ **ရောင်းချရန် ငွေပေးချေမှုစနစ် မသတ်မှတ်ရသေးပါ။**\nApp ထဲသို့ဝင်၍ KPay/Wave သတ်မှတ်ပေးပါ။", parse_mode="Markdown")

    product_type = "ebook" if message.content_type == 'document' else "audio"
    file_id = message.document.file_id if message.content_type == 'document' else message.audio.file_id
    
    pending_digital_uploads[str(message.from_user.id)] = {
        "type": product_type,
        "file_id": file_id,
        "caption": message.caption or ""
    }
    
    bot.reply_to(message, f"📥 **{product_type.upper()} File လက်ခံရရှိပါသည်!**\n\n📸 ကျေးဇူးပြု၍ ဤ E-book / Audio Book အတွက် အက်ပ်ထဲတွင်ပြသမည့် **မျက်နှာဖုံးပုံ (Cover Photo)** ကို ယခု ဆက်လက် ပေးပို့ပါ။\n\n_(စာအုပ်အမည်နှင့် ဈေးနှုန်းတို့ကို Cover Photo ပို့ရာတွင် Caption အဖြစ် ရေးသားပေးပါ)_", parse_mode="Markdown")

# 📌 STEP 2: Cover Photo ကိုလက်ခံပြီး သိမ်းဆည်းမည် (Physical Product ဆိုလျှင်လည်း ဤနေရာမှ တိုက်ရိုက်အလုပ်လုပ်မည်)
@bot.message_handler(content_types=['photo'])
def handle_photo_cover(message):
    user_tg_id = str(message.from_user.id)
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == user_tg_id).first()
    
    if not user or not (user.accept_cod or user.kpay_phone or user.wave_phone or user.kpay_qr):
        db.close()
        return bot.reply_to(message, "⚠️ App ထဲသို့ဝင်၍ Profile Setting တွင် ငွေချေစနစ် သတ်မှတ်ပေးပါ။", parse_mode="Markdown")

    if user.role == "buyer": user.role = "vendor"; db.commit()

    product_type = "physical"
    digital_file_id = ""
    caption = message.caption or ""
    image_file_id = message.photo[-1].file_id

    # Check if this photo is a cover for a previously sent digital file
    if user_tg_id in pending_digital_uploads:
        pending_data = pending_digital_uploads.pop(user_tg_id)
        product_type = pending_data["type"]
        digital_file_id = pending_data["file_id"]
        if not caption and pending_data["caption"]:
            caption = pending_data["caption"] # Fallback to document caption if photo has none

    if not caption: caption = "New Digital Product" if product_type != "physical" else "New Product"

    try:
        ai_data = {"name": caption[:30] + "..." if len(caption) > 30 else caption, "price": 0, "category": "E-Book" if product_type=="ebook" else "Audio" if product_type=="audio" else "General", "description": caption, "stock": 99999 if product_type != "physical" else 10}
        
        if GROQ_API_KEY:
            try:
                msg = bot.reply_to(message, "⏳ AI ဖြင့် အချက်အလက် ခွဲခြမ်းစိတ်ဖြာနေပါသည်...")
                headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
                prompt = f"""Analyze the Burmese text for a product: "{caption}". Extract details to strictly JSON. Required keys: 'name', 'price' (numeric), 'category', 'description'."""
                res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}).json()
                parsed = json.loads(res['choices'][0]['message']['content'])
                for k in ['name', 'price', 'category', 'description']:
                    if parsed.get(k): ai_data[k] = parsed[k]
                bot.delete_message(message.chat.id, msg.message_id)
            except: pass

        new_product = Product(name=ai_data['name'], price=float(ai_data['price']), description=ai_data['description'], category=ai_data['category'], stock=int(ai_data['stock']), image_file_id=image_file_id, vendor_id=user.id, product_type=product_type, digital_file_id=digital_file_id)
        db.add(new_product); db.commit()
        bot.reply_to(message, f"✅ **ပစ္စည်း တင်ပြီးပါပြီ။**\n\n📌 {ai_data['name']}\n💰 {ai_data['price']} Ks\n📦 အမျိုးအစား: {product_type.upper()}\n\n_(ဝယ်သူငွေချေပြီးသည်နှင့် ဖိုင်ကို လျှို့ဝှက်စွာဖြင့် တိုက်ရိုက် ပို့ပေးသွားပါမည်။)_", parse_mode="Markdown")
    except Exception as e: bot.reply_to(message, f"အမှားအယွင်း ဖြစ်ပေါ်ခဲ့ပါသည်။")
    finally: db.close()

# ==========================================
# ၆။ FRONTEND UI (100% Fixed Rendering Issues)
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
        <title>Digital Mall Master</title>
        <style>
            body { font-family: 'Inter', 'Noto Sans Myanmar', sans-serif; -webkit-tap-highlight-color: transparent; background-color: #f8fafc; overflow-x: hidden; }
            @keyframes fadeUp { 0% { opacity: 0; transform: translateY(15px); } 100% { opacity: 1; transform: translateY(0); } }
            .animate-fade-up { animation: fadeUp 0.4s ease-out forwards; }
            @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
            .animate-fade-in { animation: fadeIn 0.3s ease-in-out; }
            @keyframes float { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-5px); } }
            .animate-float { animation: float 3s ease-in-out infinite; }
            @keyframes bounceShort { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.2); } }
            .animate-bounce-short { animation: bounceShort 0.3s ease-out; }
            .gradient-text { background: linear-gradient(135deg, #2563eb, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .gradient-bg { background: linear-gradient(135deg, #2563eb, #8b5cf6); }
            .glass-header { background: rgba(255, 255, 255, 0.85); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border-bottom: 1px solid rgba(226, 232, 240, 0.8); }
            .glass-bottom-nav { background: rgba(255, 255, 255, 0.90); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border-top: 1px solid rgba(226, 232, 240, 0.8); padding-bottom: env(safe-area-inset-bottom); }
            .btn-press:active { transform: scale(0.95); transition: transform 0.1s ease; }
            .tab-btn { color: #64748b; transition: all 0.2s ease; }
            .tab-btn.active { color: #4f46e5; }
            .cat-chip { transition: all 0.2s ease; border: 1px solid #e2e8f0; }
            .cat-chip.active { background: linear-gradient(135deg, #2563eb, #8b5cf6); color: white; border-color: transparent; box-shadow: 0 4px 6px -1px rgba(99, 102, 241, 0.2); }
            .badge { position: absolute; top: -3px; right: -3px; background: #ef4444; color: white; border-radius: 50%; padding: 2px 6px; font-size: 10px; font-weight: 800; box-shadow: 0 2px 4px rgba(239,68,68,0.3); }
            .chat-bubble { max-width: 82%; padding: 10px 14px; border-radius: 18px; font-size: 13px; line-height: 1.6; word-wrap: break-word; }
            .chat-me { background: linear-gradient(135deg, #4f46e5, #6366f1); color: white; border-bottom-right-radius: 4px; box-shadow: 0 4px 10px rgba(99, 102, 241, 0.2); }
            .chat-other { background: white; color: #1e293b; border: 1px solid #e2e8f0; border-bottom-left-radius: 4px; box-shadow: 0 2px 6px rgba(0,0,0,0.03); }
            .role-badge { font-size: 9px; font-weight: 800; padding: 2px 6px; border-radius: 4px; margin-left: 6px; letter-spacing: 0.5px; }
            .role-admin { background-color: #fef08a; color: #854d0e; }
            .role-vendor { background-color: #e0e7ff; color: #3730a3; }
            .chat-input-container { position: fixed; bottom: 65px; left: 0; right: 0; background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(8px); padding: 12px; border-top: 1px solid #f1f5f9; z-index: 40; }
            .modal-overlay { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.6); backdrop-filter: blur(4px); z-index: 60; display: none; align-items: center; justify-content: center; padding: 20px; }
            .modal-overlay.active { display: flex; animation: fadeIn 0.2s ease-out; }
            .slide-up-modal { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.5); z-index: 70; display: none; flex-direction: column; justify-content: flex-end; }
            .slide-up-modal.active { display: flex; animation: fadeIn 0.2s; }
            .slide-up-content { background: white; border-radius: 24px 24px 0 0; padding: 24px; max-height: 80vh; overflow-y: auto; animation: slideUp 0.3s cubic-bezier(0.16, 1, 0.3, 1); }
            @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }
            #toast { visibility: hidden; min-width: 250px; background: rgba(15, 23, 42, 0.95); color: #fff; text-align: center; border-radius: 16px; padding: 14px 20px; position: fixed; z-index: 100; left: 50%; bottom: 85px; transform: translateX(-50%); font-size: 13px; font-weight: 600; box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1); }
            #toast.show { visibility: visible; animation: fadein 0.3s, fadeout 0.3s 3.5s; }
            .tracker-container { display: flex; justify-content: space-between; align-items: center; position: relative; margin: 15px 10px 10px 10px; }
            .tracker-line { position: absolute; top: 12px; left: 0; right: 0; height: 4px; background-color: #f1f5f9; border-radius: 2px; z-index: 1; }
            .tracker-progress { position: absolute; top: 12px; left: 0; height: 4px; border-radius: 2px; background: linear-gradient(90deg, #3b82f6, #8b5cf6); z-index: 2; transition: width 0.5s ease-in-out; }
            .track-step { position: relative; z-index: 3; display: flex; flex-direction: column; align-items: center; gap: 6px; }
            .track-dot { width: 28px; height: 28px; border-radius: 50%; background-color: white; border: 3px solid #e2e8f0; display: flex; align-items: center; justify-content: center; font-size: 12px; color: white; transition: all 0.3s; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
            .track-step.active .track-dot { border-color: transparent; background: linear-gradient(135deg, #3b82f6, #8b5cf6); box-shadow: 0 4px 6px rgba(139, 92, 246, 0.3); }
            .track-label { font-size: 10px; font-weight: 800; color: #94a3b8; }
            .track-step.active .track-label { color: #4f46e5; }
        </style>
    </head>
    <body class="pb-20">
        
        <header class="glass-header p-4 sticky top-0 z-40 flex justify-between items-center">
            <span class="font-extrabold text-xl tracking-tight gradient-text">Digital<span class="text-slate-800">Mall</span></span>
            <div class="flex items-center gap-3">
                <button onclick="openNotiModal()" class="btn-press relative p-2.5 rounded-full bg-slate-100 text-slate-600 hover:bg-slate-200 transition">
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" /></svg>
                    <span id="noti-count" class="badge hidden">0</span>
                </button>
                <button onclick="showTab('cart-tab', 'btn-cart')" class="btn-press relative p-2.5 rounded-full bg-indigo-50 text-indigo-600 hover:bg-indigo-100 transition">
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 3h2l.4 2M7 13h10l4-8H5.4M7 13L5.4 5M7 13l-2.293 2.293c-.63.63-.184 1.707.707 1.707H17m0 0a2 2 0 100 4 2 2 0 000-4zm-8 2a2 2 0 11-4 0 2 2 0 014 0z" /></svg>
                    <span id="cart-count" class="badge hidden">0</span>
                </button>
            </div>
        </header>

        <div class="glass-bottom-nav fixed bottom-0 w-full flex justify-around text-[10px] font-bold z-50 shadow-[0_-8px_20px_rgba(0,0,0,0.04)]">
            <button id="btn-shop" onclick="showTab('shop-tab', 'btn-shop')" class="tab-btn btn-press active flex-1 py-2.5 flex flex-col items-center gap-1"><span class="text-[20px] leading-none">🏠</span><span>ဝယ်မည်</span></button>
            <button id="btn-chat" onclick="showTab('chat-tab', 'btn-chat'); scrollToChatBottom();" class="tab-btn btn-press flex-1 py-2.5 flex flex-col items-center gap-1"><span class="text-[20px] leading-none">💬</span><span>စကားပြော</span></button>
            <button onclick="triggerSell()" class="tab-btn btn-press flex-1 py-2.5 flex flex-col items-center relative"><div class="absolute -top-6 gradient-bg text-white w-12 h-12 rounded-full flex items-center justify-center shadow-[0_8px_16px_rgba(99,102,241,0.3)] border-[3px] border-[#f8fafc] text-2xl pb-1 animate-float z-10">+</div><span class="mt-6 font-extrabold gradient-text">ရောင်းမည်</span></button>
            <button id="btn-history" onclick="showTab('history-tab', 'btn-history')" class="tab-btn btn-press flex-1 py-2.5 flex flex-col items-center gap-1"><span class="text-[20px] leading-none">📋</span><span>မှတ်တမ်း</span></button>
            <button id="btn-orders" onclick="showTab('orders-tab', 'btn-orders')" class="tab-btn btn-press hidden flex-1 py-2.5 flex flex-col items-center gap-1"><span class="text-[20px] leading-none">⚙️</span><span>စီမံရန်</span></button>
        </div>

        <div id="noti-modal" class="slide-up-modal" onclick="closeNotiModal(event)">
            <div class="slide-up-content" onclick="event.stopPropagation()">
                <div class="flex justify-between items-center mb-5"><h2 class="font-extrabold text-xl text-slate-800">🔔 အသိပေးချက်များ</h2><button onclick="document.getElementById('noti-modal').classList.remove('active')" class="btn-press text-slate-400 bg-slate-100 rounded-full w-8 h-8 flex items-center justify-center font-bold text-xl">&times;</button></div>
                <div id="noti-list" class="space-y-3 pb-5 min-h-[200px]"></div>
            </div>
        </div>

        <div id="edit-product-modal" class="modal-overlay">
            <div class="bg-white w-full max-w-sm rounded-[24px] p-6 shadow-2xl relative animate-fade-up">
                <button onclick="closeEditModal()" class="absolute top-4 right-4 text-slate-400 bg-slate-100 rounded-full w-8 h-8 flex items-center justify-center font-bold text-xl btn-press">&times;</button>
                <h2 class="font-extrabold text-lg mb-5 text-slate-800">ပစ္စည်း အချက်အလက် ပြင်မည်</h2>
                <input type="hidden" id="edit-prod-id">
                <div class="space-y-4">
                    <div><label class="block text-xs font-bold text-slate-500 mb-1.5">ပစ္စည်း အမည်</label><input type="text" id="edit-prod-name" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition"></div>
                    <div><label class="block text-xs font-bold text-slate-500 mb-1.5">ဈေးနှုန်း (Ks)</label><input type="number" id="edit-prod-price" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition"></div>
                    <div><label class="block text-xs font-bold text-slate-500 mb-1.5">လက်ကျန် (Stock)</label><input type="number" id="edit-prod-stock" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition"></div>
                </div>
                <button onclick="saveEditProduct()" class="btn-press w-full gradient-bg text-white font-bold py-3.5 rounded-xl shadow-[0_4px_12px_rgba(99,102,241,0.3)] mt-6 text-sm">သိမ်းဆည်းမည်</button>
            </div>
        </div>

        <div id="shop-tab" class="tab-content animate-fade-in">
            <div class="p-4 bg-white shadow-sm border-b border-slate-100 mb-3 rounded-b-3xl">
                <div class="relative mb-4"><span class="absolute left-4 top-3 text-slate-400">🔍</span><input type="text" id="search-box" oninput="autoSearch()" placeholder="ရှာဖွေလိုသော ပစ္စည်းအမည်..." class="w-full py-3 pl-10 pr-4 bg-slate-50 rounded-2xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500 outline-none transition-all font-medium"></div>
                <div id="category-container" class="flex gap-2.5 overflow-x-auto pb-2 scrollbar-hide pt-1"></div>
            </div>
            <div id="product-list" class="p-4 grid grid-cols-2 gap-4 pb-12"></div>
        </div>

        <div id="chat-tab" class="tab-content hidden animate-fade-in flex flex-col" style="height: calc(100vh - 150px);">
            <div id="chat-messages" class="flex-1 overflow-y-auto p-4 space-y-4 pb-10"><div class="text-center text-xs font-bold text-slate-400 my-4">-- ယခု Chat သည် Public ဖြစ်ပါသည် --</div></div>
            <div class="chat-input-container flex gap-2 items-end">
                <textarea id="chat-input" rows="1" class="flex-1 bg-white border border-slate-200 rounded-2xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 shadow-inner resize-none max-h-24" placeholder="စာရိုက်ပါ..."></textarea>
                <button onclick="sendChatMessage()" class="w-12 h-12 bg-indigo-600 text-white rounded-full flex items-center justify-center shrink-0 btn-press shadow-[0_4px_10px_rgba(79,70,229,0.4)] transition hover:bg-indigo-700"><svg class="w-5 h-5 ml-1" fill="currentColor" viewBox="0 0 20 20"><path d="M10.894 2.553a1 1 0 00-1.788 0l-7 14a1 1 0 001.169 1.409l5-1.429A1 1 0 009 15.571V11a1 1 0 112 0v4.571a1 1 0 00.725.962l5 1.428a1 1 0 001.17-1.408l-7-14z"></path></svg></button>
            </div>
        </div>

        <div id="cart-tab" class="tab-content hidden p-4 animate-fade-in">
            <h2 class="font-extrabold text-slate-800 text-2xl mb-5 flex items-center gap-2">🛒 သင်၏ခြင်းတောင်း</h2>
            <div id="cart-empty-state" class="hidden animate-fade-up bg-white p-12 rounded-3xl shadow-sm border border-slate-100 text-center text-slate-400 mt-10"><div class="text-6xl mb-4 animate-float">🛍️</div><p class="font-bold text-slate-500">ပစ္စည်းမရှိသေးပါ</p></div>
            <div id="cart-content-wrapper" class="pb-10"></div>
        </div>

        <div id="history-tab" class="tab-content hidden p-4 animate-fade-in"><h2 class="font-extrabold text-slate-800 text-2xl mb-5">📋 အော်ဒါမှတ်တမ်းများ</h2><div id="buyer-order-list" class="space-y-4 pb-10"></div></div>
        
        <div id="orders-tab" class="tab-content hidden p-4 animate-fade-in">
            <div class="flex bg-slate-100 p-1.5 rounded-2xl mb-5 shadow-inner">
                <button onclick="switchVendorTab('dash')" id="v-tab-dash" class="btn-press flex-1 bg-white shadow-sm py-2.5 rounded-xl text-sm font-extrabold text-indigo-600 transition-all">အော်ဒါများ</button>
                <button onclick="switchVendorTab('prods')" id="v-tab-prods" class="btn-press flex-1 py-2.5 rounded-xl text-sm font-bold text-slate-500 transition-all">ပစ္စည်းများ</button>
                <button onclick="switchVendorTab('profile')" id="v-tab-profile" class="btn-press flex-1 py-2.5 rounded-xl text-sm font-bold text-slate-500 transition-all">Profile</button>
            </div>
            <div id="vendor-dash-view" class="animate-fade-up"><div id="order-list" class="space-y-4 pb-10"></div></div>
            <div id="vendor-prods-view" class="hidden animate-fade-up"><div id="vendor-product-list" class="space-y-3 pb-10"></div></div>
            <div id="vendor-profile-view" class="hidden animate-fade-up">
                <div class="bg-white p-6 rounded-3xl shadow-sm border border-slate-100 pb-10">
                    <h3 class="font-extrabold text-slate-800 text-lg mb-5 flex items-center gap-2">🏪 ရောင်းသူ Profile </h3>
                    <label class="flex items-center gap-3 p-4 bg-indigo-50/50 rounded-2xl border border-indigo-100 mb-5 cursor-pointer transition hover:bg-indigo-50">
                        <input type="checkbox" id="prof-cod" class="w-5 h-5 text-indigo-600 rounded border-gray-300 focus:ring-indigo-500"><span class="font-bold text-sm text-indigo-900">အိမ်ရောက်မှ ငွေချေစနစ် (COD)</span>
                    </label>
                    <div class="space-y-5">
                        <div class="bg-slate-50 p-4 rounded-2xl border border-slate-200">
                            <label class="block text-xs font-extrabold text-indigo-700 mb-2">KPay ဖုန်း / QR</label>
                            <input type="text" id="prof-kpay-ph" placeholder="09xxxxxxxxx" class="w-full p-3 bg-white rounded-xl border border-slate-200 mb-3 text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition">
                            <input type="file" accept="image/*" onchange="encodeImage(this, 'prof-kpay-qr')" class="text-xs text-slate-500 file:mr-4 file:py-2 file:px-4 file:rounded-full file:border-0 file:text-xs file:font-bold file:bg-indigo-50 file:text-indigo-700 hover:file:bg-indigo-100 transition">
                            <input type="hidden" id="prof-kpay-qr"><img id="prof-kpay-preview" class="h-24 object-cover rounded-xl hidden border border-slate-200 shadow-sm mt-3">
                        </div>
                        <div class="bg-slate-50 p-4 rounded-2xl border border-slate-200">
                            <label class="block text-xs font-extrabold text-amber-600 mb-2">WavePay ဖုန်း / QR</label>
                            <input type="text" id="prof-wave-ph" placeholder="09xxxxxxxxx" class="w-full p-3 bg-white rounded-xl border border-slate-200 mb-3 text-sm outline-none focus:ring-2 focus:ring-amber-500 transition">
                            <input type="file" accept="image/*" onchange="encodeImage(this, 'prof-wave-qr')" class="text-xs text-slate-500 file:mr-4 file:py-2 file:px-4 file:rounded-full file:border-0 file:text-xs file:font-bold file:bg-amber-50 file:text-amber-700 hover:file:bg-amber-100 transition">
                            <input type="hidden" id="prof-wave-qr"><img id="prof-wave-preview" class="h-24 object-cover rounded-xl hidden border border-slate-200 shadow-sm mt-3">
                        </div>
                    </div>
                    <button onclick="saveVendorProfile()" class="btn-press w-full mt-6 gradient-bg text-white font-bold py-3.5 rounded-xl shadow-[0_4px_12px_rgba(99,102,241,0.3)] text-sm">သိမ်းဆည်းမည်</button>
                </div>
            </div>
        </div>

        <div id="setup-modal" class="modal-overlay">
            <div class="bg-white w-full max-w-sm rounded-[24px] p-6 shadow-2xl relative animate-fade-up">
                <button onclick="document.getElementById('setup-modal').classList.remove('active')" class="btn-press absolute top-4 right-4 text-slate-400 bg-slate-100 rounded-full w-8 h-8 flex items-center justify-center font-bold text-xl">&times;</button>
                <div class="text-4xl mb-3">🏪</div><h2 class="font-extrabold text-xl mb-2 text-slate-800">ဆိုင်ဖွင့်ရန် လိုအပ်ချက်များ</h2>
                <p class="text-sm text-slate-500 mb-6 font-medium leading-relaxed">ပစ္စည်းမတင်မီ သင်လက်ခံမည့် ငွေချေစနစ် (KPay, Wave, COD) များကို အရင်သတ်မှတ်ပေးပါ။</p>
                <button onclick="document.getElementById('setup-modal').classList.remove('active'); showTab('orders-tab', 'btn-orders'); switchVendorTab('profile');" class="btn-press w-full gradient-bg text-white font-bold py-3.5 rounded-xl shadow-md text-sm">Setting သို့သွားရန်</button>
            </div>
        </div>

        <div id="toast">Message</div>

        <script>
            const tg = window.Telegram.WebApp; const initData = tg.initData; 
            let allProducts = [], currentCategory = 'All', cart = []; let searchTimeout = null, mmData = {}; let currentUser = {}; let chatWs = null;
            let currentVendorProducts = []; // To safely edit products without breaking JSON

            function showToast(msg) {
                const t = document.getElementById("toast"); t.innerText = msg; t.className = "show animate-bounce-short";
                if(tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
                setTimeout(() => { t.className = t.className.replace("show animate-bounce-short", ""); }, 3200);
            }
            async function apiFetch(url, options = {}) { return fetch(url, { ...options, headers: { 'X-Telegram-Init-Data': initData, 'Content-Type': 'application/json', ...options.headers }}); }

            async function initApp() {
                tg.expand(); tg.ready(); fetchLocationData(); fetchNotifications();
                try {
                    const res = await apiFetch('/api/auth'); const data = await res.json(); currentUser = data.user;
                    document.getElementById('prof-cod').checked = currentUser.accept_cod; document.getElementById('prof-kpay-ph').value = currentUser.kpay_phone || ''; document.getElementById('prof-wave-ph').value = currentUser.wave_phone || '';
                    if (currentUser.role === 'vendor' || currentUser.role === 'admin') { document.getElementById('btn-orders').classList.remove('hidden'); }
                    loadProducts(); initChat();
                } catch (e) { showToast("Authentication Failed"); }
            }

            async function fetchLocationData() { try { const res = await fetch('/api/locations'); mmData = await res.json(); } catch(e) {} }
            function getSelectOptions(dataObj, defaultText) { let html = `<option value="">-- ${defaultText} --</option>`; if(dataObj) { if(Array.isArray(dataObj)) dataObj.forEach(v => html += `<option value="${v}">${v}</option>`); else for(let k in dataObj) html += `<option value="${k}">${k}</option>`; } return html; }
            
            function showTab(tabId, btnId) {
                if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
                document.querySelectorAll('.tab-content').forEach(el => { el.classList.add('hidden'); el.classList.remove('animate-fade-in'); });
                document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
                const tab = document.getElementById(tabId); tab.classList.remove('hidden'); void tab.offsetWidth; tab.classList.add('animate-fade-in');
                if(btnId) { document.getElementById(btnId).classList.add('active'); document.getElementById('btn-cart').classList.remove('active'); }
                if(tabId === 'cart-tab') { document.getElementById('btn-cart').classList.add('active'); renderGroupedCart(); }
                if(tabId === 'history-tab') loadBuyerOrders(); if(tabId === 'orders-tab') switchVendorTab('dash');
                if(tabId !== 'chat-tab') window.scrollTo({ top: 0, behavior: 'smooth' }); else scrollToChatBottom();
            }

            async function initChat() {
                try { const res = await apiFetch('/api/chat/history'); const history = await res.json(); const container = document.getElementById('chat-messages'); history.forEach(m => container.insertAdjacentHTML('beforeend', renderMessage(m))); scrollToChatBottom(); connectWebSocket(); } catch(e) {}
                document.getElementById('chat-input').addEventListener('keydown', function(e) { if(e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChatMessage(); } });
            }
            function connectWebSocket() {
                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'; const wsUrl = `${protocol}//${window.location.host}/ws/chat?tg_data=${encodeURIComponent(initData)}`;
                chatWs = new WebSocket(wsUrl);
                chatWs.onmessage = (event) => { const msg = JSON.parse(event.data); document.getElementById('chat-messages').insertAdjacentHTML('beforeend', renderMessage(msg)); scrollToChatBottom(); };
                chatWs.onclose = () => { setTimeout(() => connectWebSocket(), 3000); };
            }
            function renderMessage(m) {
                const isMe = m.sender_tg === currentUser.id; let roleBadge = ''; if(m.sender_role === 'admin') roleBadge = '<span class="role-badge role-admin">Admin</span>'; else if(m.sender_role === 'vendor') roleBadge = '<span class="role-badge role-vendor">Vendor</span>';
                if(isMe) { return `<div class="flex justify-end animate-fade-up"><div class="flex flex-col items-end"><div class="chat-bubble chat-me">${escapeHTML(m.text)}</div><span class="text-[9px] font-bold text-slate-400 mt-1 mr-1">${m.time}</span></div></div>`; } 
                else { const initial = m.sender_name.charAt(0).toUpperCase(); return `<div class="flex justify-start animate-fade-up"><div class="w-8 h-8 rounded-full bg-indigo-50 flex items-center justify-center text-indigo-600 font-black text-xs shrink-0 mr-2 border border-indigo-100 mt-1">${initial}</div><div class="flex flex-col items-start max-w-[80%]"><div class="text-[10px] font-extrabold text-slate-500 mb-1 ml-1 flex items-center tracking-wide">${m.sender_name} ${roleBadge}</div><div class="chat-bubble chat-other">${escapeHTML(m.text)}</div><span class="text-[9px] font-bold text-slate-400 mt-1 ml-1">${m.time}</span></div></div>`; }
            }
            function sendChatMessage() { const input = document.getElementById('chat-input'); const text = input.value.trim(); if(!text || !chatWs || chatWs.readyState !== WebSocket.OPEN) return; chatWs.send(text); input.value = ''; scrollToChatBottom(); }
            function scrollToChatBottom() { setTimeout(() => { const c = document.getElementById('chat-tab'); c.scrollTop = c.scrollHeight; }, 100); }
            function escapeHTML(str) { return str.replace(/[&<>'"]/g, tag => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[tag])); }

            async function fetchNotifications() {
                try {
                    const res = await apiFetch('/api/notifications'); const notis = await res.json(); const badge = document.getElementById('noti-count'); const unread = notis.filter(n => !n.is_read).length;
                    if(unread > 0) { badge.innerText = unread; badge.classList.remove('hidden'); badge.classList.add('animate-bounce-short'); } else { badge.classList.add('hidden'); }
                    const list = document.getElementById('noti-list'); if(notis.length === 0) return list.innerHTML = `<div class="text-center text-slate-400 py-10 font-medium">အသိပေးချက် မရှိသေးပါ</div>`;
                    list.innerHTML = notis.map(n => `<div class="p-4 rounded-2xl border transition-all ${n.is_read ? 'bg-slate-50 border-slate-100 text-slate-500' : 'bg-indigo-50 border-indigo-100 text-indigo-900 shadow-sm'}"><div class="font-bold text-[13px] mb-1.5">${n.message}</div><div class="text-[10px] font-bold ${n.is_read ? 'text-slate-400' : 'text-indigo-500'}">${n.date}</div></div>`).join('');
                } catch(e) {}
            }
            function openNotiModal() { if(tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium'); document.getElementById('noti-modal').classList.add('active'); apiFetch('/api/notifications/read', { method: 'POST' }).then(() => document.getElementById('noti-count').classList.add('hidden')); }
            function closeNotiModal(e) { if(e.target === document.getElementById('noti-modal')) { document.getElementById('noti-modal').classList.remove('active'); fetchNotifications(); } }

            function triggerSell() { if(!currentUser.vendor_ready) document.getElementById('setup-modal').classList.add('active'); else tg.showConfirm("Digital ပစ္စည်း (Ebook/Audio) ရောင်းလိုပါက ဖိုင်ကိုအရင်ပို့ပြီးမှ မျက်နှာဖုံးပုံကို နောက်မှပို့ပါ။ ရုပ်ဝတ္ထုဆိုပါက ပုံကိုတန်းပို့ပါ။ Bot ဆီသွားမလား?", (r) => { if(r) tg.close(); }); }
            function encodeImage(el, targetId) { let f = el.files[0]; if(!f) return; let r = new FileReader(); r.onloadend = function() { document.getElementById(targetId).value = r.result; document.getElementById(targetId + '-preview').src = r.result; document.getElementById(targetId + '-preview').classList.remove('hidden'); }; r.readAsDataURL(f); }
            async function saveVendorProfile() {
                tg.MainButton.showProgress(); const payload = { accept_cod: document.getElementById('prof-cod').checked, kpay_phone: document.getElementById('prof-kpay-ph').value, wave_phone: document.getElementById('prof-wave-ph').value, kpay_qr: document.getElementById('prof-kpay-qr').value, wave_qr: document.getElementById('prof-wave-qr').value };
                const res = await apiFetch('/api/vendor/profile', { method: 'POST', body: JSON.stringify(payload) }); tg.MainButton.hideProgress();
                if(res.ok) { showToast("Profile သိမ်းဆည်းပြီးပါပြီ။"); currentUser.vendor_ready = true; document.getElementById('btn-orders').classList.remove('hidden'); }
            }

            // 📌 Fixed Frontend Bug by using Array Indexes
            async function loadProducts(query = "") {
                const res = await apiFetch(`/api/products?category=${currentCategory}&search=${query}`); const data = await res.json(); allProducts = data.products;
                if(query === "") {
                    let catsHTML = `<button onclick="filterCategory('All')" class="btn-press cat-chip ${currentCategory==='All'?'active':''} px-5 py-2.5 rounded-full text-xs font-bold bg-white whitespace-nowrap shadow-sm text-slate-600">အားလုံး</button>`;
                    data.categories.forEach(c => catsHTML += `<button onclick="filterCategory('${c}')" class="btn-press cat-chip ${currentCategory===c?'active':''} px-5 py-2.5 rounded-full text-xs font-bold bg-white whitespace-nowrap shadow-sm text-slate-600">${c}</button>`);
                    document.getElementById('category-container').innerHTML = catsHTML;
                }
                if(allProducts.length === 0) return document.getElementById('product-list').innerHTML = `<div class="col-span-2 text-center py-12 text-slate-400 font-medium animate-fade-up">ပစ္စည်းရှာမတွေ့ပါ 🔍</div>`;
                
                document.getElementById('product-list').innerHTML = allProducts.map((p, index) => {
                    const imgSrc = p.img ? `/api/image/${p.img}` : 'https://via.placeholder.com/300?text=Digital+Product'; 
                    const isDigital = p.type !== 'physical'; const isOut = !isDigital && p.stock <= 0; const animDelay = (index % 10) * 0.05;
                    
                    let typeBadgeHtml = '';
                    if (p.type === 'ebook') typeBadgeHtml = `<div class="text-[9px] font-bold text-blue-600 bg-blue-50 px-1.5 py-0.5 rounded-md shrink-0 border border-blue-100">📚 E-Book</div>`;
                    else if (p.type === 'audio') typeBadgeHtml = `<div class="text-[9px] font-bold text-purple-600 bg-purple-50 px-1.5 py-0.5 rounded-md shrink-0 border border-purple-100">🎧 Audio</div>`;
                    else typeBadgeHtml = `<div class="text-[9px] font-bold ${isOut ? 'text-red-500 bg-red-50' : 'text-emerald-600 bg-emerald-50'} px-1.5 py-0.5 rounded-md shrink-0 border ${isOut ? 'border-red-100' : 'border-emerald-100'}">📦 Stock: ${p.stock}</div>`;

                    return `<div class="animate-fade-up bg-white rounded-[24px] shadow-[0_4px_16px_rgba(0,0,0,0.03)] border border-slate-100 overflow-hidden flex flex-col relative transition-all duration-300 transform hover:-translate-y-1 hover:shadow-xl ${isOut ? 'opacity-60 grayscale-[30%]' : ''}" style="animation-delay: ${animDelay}s">
                        ${isOut ? '<div class="absolute top-2 right-2 bg-red-500/90 backdrop-blur text-white text-[10px] font-black px-2 py-1 rounded-lg z-30 shadow-sm">ကုန်နေပါသည်</div>' : ''}
                        <div class="pt-5 pb-3 px-4 bg-slate-50 border-b border-slate-100/50 flex flex-col items-center justify-center relative overflow-hidden shrink-0"><div class="absolute bottom-0 w-full h-1/3 bg-gradient-to-t from-slate-200/50 to-transparent"></div><div class="absolute bottom-2.5 w-20 h-2 bg-slate-300/60 blur-[4px] rounded-full"></div><img src="${imgSrc}" class="w-[90px] h-[90px] object-cover rounded-[18px] shadow-[0_8px_16px_rgba(0,0,0,0.08)] border-2 border-white relative z-10 transform transition-transform duration-300 hover:scale-105 bg-white"></div>
                        <div class="p-3.5 flex-grow flex flex-col justify-between bg-white z-20">
                            <div class="mb-2">
                                <div class="text-[9px] font-extrabold text-slate-400 uppercase tracking-wider line-clamp-1 mb-1">🏪 ${escapeHTML(p.vendor_name)}</div>
                                <div class="text-[13px] font-extrabold text-slate-800 line-clamp-1 leading-snug mb-1.5">${escapeHTML(p.name)}</div>
                                <div class="flex justify-between items-end mb-2"><div class="text-indigo-600 text-[15px] font-black leading-none">${p.price.toLocaleString()} <span class="text-[10px] font-bold">Ks</span></div>${typeBadgeHtml}</div>
                                <div class="text-[10px] font-medium text-slate-500 line-clamp-2 leading-relaxed bg-slate-50 p-2 rounded-xl border border-slate-100/60">${p.desc ? escapeHTML(p.desc) : 'အကြောင်းအရာဖော်ပြချက် မရှိပါ။'}</div>
                            </div>
                            <button onclick="addToCartIndex(${index})" class="btn-press mt-1 w-full ${isOut?'bg-slate-100 text-slate-400':'bg-indigo-50 text-indigo-700 hover:bg-indigo-100'} py-2.5 rounded-xl font-bold text-xs transition-colors" ${isOut?'disabled':''}>🛒 ခြင်းထဲထည့်မည်</button>
                        </div>
                    </div>`
                }).join('');
            }
            function filterCategory(cat) { currentCategory = cat; loadProducts(); }
            function autoSearch() { clearTimeout(searchTimeout); searchTimeout = setTimeout(() => { loadProducts(document.getElementById('search-box').value); }, 400); }
            
            function addToCartIndex(idx) {
                let prod = allProducts[idx];
                let ex = cart.find(i => i.id === prod.id); 
                if(ex) { if(prod.type !== 'physical' || ex.qty < prod.stock) ex.qty++; else return showToast("Stock မလုံလောက်ပါ။"); } else { cart.push({...prod, qty: 1}); }
                updateCartBadge(); showToast("🛒 ခြင်းထဲရောက်ပါပြီ"); 
            }
            function updateCartBadge() { const b = document.getElementById('cart-count'); let t = cart.reduce((s, i) => s + i.qty, 0); b.innerText = t; if(t > 0) { b.classList.remove('hidden'); b.classList.remove('animate-bounce-short'); void b.offsetWidth; b.classList.add('animate-bounce-short'); } else { b.classList.add('hidden'); } }

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
                        <div class="flex-1 pr-3"><div class="text-[13px] font-bold text-slate-800 line-clamp-1">${escapeHTML(i.name)}</div><div class="text-indigo-600 text-[12px] font-black mt-0.5">${i.price.toLocaleString()} Ks</div></div>
                        <div class="flex items-center gap-3 bg-slate-50 rounded-xl shadow-inner px-2 py-1 border border-slate-100"><button onclick="changeQty(${i.cart_idx}, -1)" class="btn-press w-7 h-7 flex items-center justify-center text-slate-500 font-bold bg-white rounded-lg shadow-sm">-</button><span class="text-xs font-black w-3 text-center text-slate-700">${i.qty}</span><button onclick="changeQty(${i.cart_idx}, 1)" class="btn-press w-7 h-7 flex items-center justify-center text-indigo-600 font-bold bg-white rounded-lg shadow-sm">+</button></div>
                    </div>`).join('');
                    
                    let hasDigital = g.items.some(i => i.type !== 'physical');
                    let options = [];
                    if(g.vendor_cod && !hasDigital) options.push(`<option value="COD">🏠 အိမ်ရောက်မှ ငွေချေမည် (COD)</option>`);
                    if(g.kpay_phone || g.kpay_qr) options.push(`<option value="KPay" ${hasDigital || !g.vendor_cod ? 'selected' : ''}>📲 KPay ဖြင့် ငွေလွှဲမည်</option>`);
                    if(g.wave_phone || g.wave_qr) options.push(`<option value="Wave">📲 WavePay ဖြင့် ငွေလွှဲမည်</option>`);
                    if(options.length === 0) options.push(`<option value="COD">⚠️ ငွေချေစနစ် မသတ်မှတ်ရသေးပါ</option>`);

                    let payHtml = `<div class="mt-4 border-t border-slate-100 pt-4"><label class="text-xs font-bold text-slate-500 mb-2 block">ငွေချေစနစ်ရွေးချယ်ရန် ${hasDigital ? '<span class="text-[10px] text-red-500">(E-book/Audio အတွက် COD မရပါ)</span>' : ''}</label><select id="pay_method_${vid}" onchange="togglePayMethod(${vid})" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-[13px] font-bold mb-3 focus:ring-2 focus:ring-indigo-500 outline-none transition text-slate-700">${options.join('')}</select><div id="qr_box_${vid}" class="bg-indigo-50/50 p-4 rounded-2xl mb-4 border border-indigo-100 animate-fade-in"><div class="flex justify-center mb-3"><img id="qr_img_${vid}" src="" class="max-h-36 rounded-xl shadow-md border border-white hidden"></div><p id="qr_phone_${vid}" class="text-center font-mono font-black text-xl text-indigo-900 tracking-wider bg-white py-2 rounded-xl border border-indigo-100 shadow-sm">-</p><input type="text" id="tx_id_${vid}" placeholder="ငွေလွှဲပြေစာ (Tx ID) ၆ လုံး..." class="w-full mt-3 p-3 border border-indigo-200 rounded-xl text-sm text-center outline-none focus:ring-2 focus:ring-indigo-500 font-bold text-indigo-900 placeholder-indigo-300"></div></div>`;
                    
                    html += `<div class="animate-fade-up bg-white p-5 rounded-3xl shadow-sm border border-slate-100 mb-5"><div class="flex justify-between items-center mb-4 pb-3 border-b border-slate-50"><h3 class="font-extrabold text-slate-800 flex items-center gap-1.5"><span class="text-lg">🏪</span> ${escapeHTML(g.vendor_name)}</h3><span class="text-[15px] font-black gradient-text">${g.total.toLocaleString()} Ks</span></div>${itemsHtml}${payHtml}<button onclick="checkoutVendor(${vid})" class="btn-press w-full gradient-bg text-white font-bold py-3.5 rounded-xl shadow-[0_4px_12px_rgba(99,102,241,0.3)] mt-2 text-sm tracking-wide">အော်ဒါတင်မည်</button></div>`;
                }
                html += `<div class="animate-fade-up bg-slate-50 border border-slate-200 p-5 rounded-3xl mb-5 mt-8 shadow-inner"><h3 class="font-extrabold text-slate-700 mb-4 text-sm flex items-center gap-2"><span class="text-lg">📍</span> လိပ်စာအချက်အလက်</h3><div class="space-y-3.5"><select id="sel-state" onchange="updateAddr('sel-district', mmData[this.value])" class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-indigo-500 outline-none transition"></select><select id="sel-district" onchange="updateAddr('sel-township', mmData[document.getElementById('sel-state').value][this.value])" class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-indigo-500 outline-none transition"><option value="">-- ခရိုင် --</option></select><select id="sel-township" class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-indigo-500 outline-none transition"><option value="">-- မြို့နယ် --</option></select><input type="text" id="input-street" placeholder="အိမ်အမှတ်၊ လမ်းအမည်..." class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-indigo-500 outline-none transition"><input type="tel" id="input-phone" placeholder="ဆက်သွယ်ရမည့် ဖုန်းနံပါတ်..." value="${currentUser.phone||''}" class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-indigo-500 outline-none transition"></div></div>`;
                document.getElementById('cart-content-wrapper').innerHTML = html; 
                document.getElementById('sel-state').innerHTML = getSelectOptions(mmData, "တိုင်းဒေသကြီး");
                for(let vid in vGroups) togglePayMethod(vid, vGroups[vid]);
            }
            function updateAddr(tId, sData) { document.getElementById(tId).innerHTML = getSelectOptions(sData, "ရွေးချယ်ပါ"); }
            function changeQty(idx, d) { if(d > 0 && cart[idx].type === 'physical' && cart[idx].qty >= cart[idx].stock) return showToast("Stock မလုံလောက်ပါ။"); cart[idx].qty += d; if(cart[idx].qty <= 0) cart.splice(idx, 1); updateCartBadge(); renderGroupedCart(); }
            function togglePayMethod(vid, gData) {
                const m = document.getElementById(`pay_method_${vid}`).value; const box = document.getElementById(`qr_box_${vid}`);
                if(m === "COD") { box.classList.add("hidden"); } else {
                    box.classList.remove("hidden"); if(!gData) { let f = cart.find(i => i.vendor_id == vid); gData = {kpay_phone: f.vendor_kpay, wave_phone: f.vendor_wave, kpay_qr: f.kpay_qr, wave_qr: f.wave_qr}; }
                    const img = document.getElementById(`qr_img_${vid}`), ph = document.getElementById(`qr_phone_${vid}`);
                    if(m === "KPay") { img.src = gData.kpay_qr || ""; img.classList.toggle("hidden", !gData.kpay_qr); ph.innerText = gData.kpay_phone || "-"; } 
                    else if(m === "Wave") { img.src = gData.wave_qr || ""; img.classList.toggle("hidden", !gData.wave_qr); ph.innerText = gData.wave_phone || "-"; }
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
                    if(res.ok) { cart = cart.filter(i => i.vendor_id != vid); updateCartBadge(); showToast("✅ အော်ဒါတင်ခြင်း အောင်မြင်ပါသည်။"); if(cart.length === 0) showTab('history-tab', 'btn-history'); else renderGroupedCart(); } else { let errData = await res.json(); showToast("⚠️ " + (errData.detail || "အမှားအယွင်းဖြစ်ပေါ်ခဲ့ပါသည်။")); }
                } catch(e) { showToast("⚠️ အင်တာနက်ချိတ်ဆက်မှု ပြတ်တောက်သွားပါသည်။"); } finally { tg.MainButton.hideProgress(); }
            }

            async function loadBuyerOrders() {
                const res = await apiFetch('/api/buyer/orders'); const orders = await res.json();
                const trackIndex = { 'pending': 1, 'approved': 2, 'shipped': 3, 'delivered': 4, 'cancelled': 0 };
                document.getElementById('buyer-order-list').innerHTML = orders.map((o, index) => {
                    let level = trackIndex[o.status]; let progressWidth = level === 0 ? 0 : ((level - 1) / 3) * 100;
                    let trackerHtml = o.status === 'cancelled' ? `<div class="text-center my-4"><span class="status-cancelled">❌ ဤအော်ဒါအား ပယ်ဖျက်လိုက်ပါသည်</span></div>` : `<div class="tracker-container"><div class="tracker-line"></div><div class="tracker-progress" style="width: ${progressWidth}%;"></div><div class="track-step ${level >= 1 ? 'active' : ''}"><div class="track-dot">✓</div><span class="track-label">စစ်ဆေးဆဲ</span></div><div class="track-step ${level >= 2 ? 'active' : ''}"><div class="track-dot">📦</div><span class="track-label">အတည်ပြုသည်</span></div><div class="track-step ${level >= 3 ? 'active' : ''}"><div class="track-dot">🚚</div><span class="track-label">ပို့နေပါပြီ</span></div><div class="track-step ${level >= 4 ? 'active' : ''}"><div class="track-dot">🎁</div><span class="track-label">ရောက်ပါပြီ</span></div></div>`;
                    return `<div class="animate-fade-up bg-white p-5 rounded-3xl shadow-[0_2px_12px_rgba(0,0,0,0.03)] border border-slate-100" style="animation-delay: ${(index % 10) * 0.1}s"><div class="flex justify-between items-start mb-3 border-b border-slate-50 pb-3"><span class="text-[14px] font-extrabold text-slate-800 leading-snug">${escapeHTML(o.name)} <span class="text-indigo-600 bg-indigo-50 px-1.5 py-0.5 rounded-md text-[11px] ml-1">x${o.qty}</span></span><span class="font-black text-slate-800 ml-3 shrink-0">${(o.price * o.qty).toLocaleString()} Ks</span></div>${trackerHtml}<div class="flex justify-between items-center text-[10px] font-bold text-slate-400 mt-4 bg-slate-50 p-2 rounded-xl"><span class="flex items-center gap-1">${o.pay === 'COD' ? '🏠 COD စနစ်' : '💳 QR ဖြင့်ချေထားသည်'}</span><span>📅 ${o.date}</span></div></div>`;
                }).join('');
            }
            
            function switchVendorTab(tab) {
                ['dash', 'prods', 'profile'].forEach(t => { document.getElementById(`v-tab-${t}`).className = tab === t ? 'btn-press flex-1 bg-white shadow-[0_2px_8px_rgba(0,0,0,0.05)] py-2.5 rounded-xl text-sm font-extrabold text-indigo-600 transition-all' : 'btn-press flex-1 py-2.5 rounded-xl text-sm font-bold text-slate-500 transition-all hover:bg-slate-200/50'; document.getElementById(`vendor-${t}-view`).style.display = tab === t ? 'block' : 'none'; });
                if(tab === 'dash') loadVendorOrders(); else if(tab === 'prods') loadVendorProducts();
            }
            
            async function loadVendorOrders() {
                const res = await apiFetch('/api/vendor/orders'); const orders = await res.json();
                document.getElementById('order-list').innerHTML = orders.map((o, index) => {
                    return `<div class="animate-fade-up bg-white p-4 rounded-3xl shadow-sm border border-slate-100 mb-4" style="animation-delay: ${(index % 10) * 0.1}s"><div class="flex justify-between items-start mb-3"><div class="text-sm font-extrabold text-slate-800 pr-2">${escapeHTML(o.name)} <span class="text-indigo-600">x${o.qty}</span></div><div class="text-[10px] uppercase font-black px-2.5 py-1 rounded-lg ${o.status==='pending'?'bg-amber-100 text-amber-700':o.status==='cancelled'?'bg-red-100 text-red-700':'bg-emerald-100 text-emerald-700'}">${o.status}</div></div><div class="bg-slate-50 p-3 rounded-2xl text-[12px] font-medium text-slate-600 mb-3 border border-slate-100 space-y-1.5 leading-relaxed"><div class="flex items-center gap-1.5"><span class="text-slate-400">👤</span> ${escapeHTML(o.buyer)}</div><div class="flex items-center gap-1.5"><span class="text-slate-400">💳</span> ${o.pay === 'COD' ? '🏠 COD' : 'TxID: <b class="text-slate-800 font-mono tracking-wide">'+o.tx+'</b>'}</div><div class="flex items-start gap-1.5"><span class="text-slate-400 mt-0.5">📍</span> <span class="line-clamp-2">${escapeHTML(o.addr)}</span></div></div><select onchange="updateOrderStatus(${o.id}, this.value)" class="w-full bg-white border border-indigo-200 text-indigo-700 p-3 rounded-xl text-xs font-bold outline-none focus:ring-2 focus:ring-indigo-500 shadow-[0_2px_4px_rgba(99,102,241,0.05)] transition"><option value="pending" ${o.status==='pending'?'selected':''}>⏳ စစ်ဆေးဆဲ</option><option value="approved" ${o.status==='approved'?'selected':''}>📦 အတည်ပြုမည် / ဒစ်ဂျစ်တယ်ဖိုင်ပို့မည်</option><option value="shipped" ${o.status==='shipped'?'selected':''}>🚚 ပို့ဆောင်လိုက်ပြီ</option><option value="delivered" ${o.status==='delivered'?'selected':''}>✅ ရောက်ရှိပါပြီ</option><option value="cancelled" ${o.status==='cancelled'?'selected':''}>❌ ပယ်ဖျက်မည်</option></select></div>`}).join('');
            }
            async function updateOrderStatus(id, st) { await apiFetch(`/api/vendor/orders/${id}/status?status=${st}`, {method:'POST'}); showToast("✅ အခြေအနေ ပြောင်းလဲပြီးပါပြီ"); loadVendorOrders(); }
            
            async function loadVendorProducts() {
                const res = await apiFetch('/api/vendor/products'); currentVendorProducts = await res.json();
                document.getElementById('vendor-product-list').innerHTML = currentVendorProducts.map((p, index) => {
                    const imgSrc = p.img ? `/api/image/${p.img}` : 'https://via.placeholder.com/300?text=Digital';
                    return `<div class="animate-fade-up bg-white p-3.5 rounded-3xl shadow-sm border border-slate-100 flex gap-4 items-center mb-3" style="animation-delay: ${(index % 10) * 0.1}s"><img src="${imgSrc}" class="w-20 h-20 object-cover rounded-2xl shadow-sm border border-slate-100 bg-slate-50"><div class="flex-1"><div class="text-[13px] font-extrabold text-slate-800 line-clamp-1 mb-1">${escapeHTML(p.name)}</div><div class="text-indigo-600 text-sm font-black mb-1.5">${p.price.toLocaleString()} Ks</div><div class="text-[10px] text-slate-500 font-extrabold bg-slate-100 inline-flex px-2 py-0.5 rounded-md items-center gap-1 border border-slate-200">📦 Type: ${p.type.toUpperCase()}</div></div><div class="flex flex-col gap-2"><button onclick="openEditModalIndex(${index})" class="btn-press bg-indigo-50 text-indigo-600 px-4 py-1.5 rounded-xl text-[11px] font-extrabold transition hover:bg-indigo-100">ပြင်မည်</button><button onclick="if(confirm('ဤပစ္စည်းကို ဖျက်မှာ သေချာပါသလား?')) apiFetch('/api/vendor/products/${p.id}', {method:'DELETE'}).then(loadVendorProducts)" class="btn-press text-red-500 bg-red-50 px-4 py-1.5 rounded-xl text-[11px] font-extrabold transition hover:bg-red-100">ဖျက်မည်</button></div></div>`
                }).join('');
            }
            function openEditModalIndex(idx) { let prod = currentVendorProducts[idx]; document.getElementById('edit-prod-id').value = prod.id; document.getElementById('edit-prod-name').value = prod.name; document.getElementById('edit-prod-price').value = prod.price; document.getElementById('edit-prod-stock').value = prod.stock; document.getElementById('edit-product-modal').classList.add('active'); }
            function closeEditModal() { document.getElementById('edit-product-modal').classList.remove('active'); }
            async function saveEditProduct() {
                const id = document.getElementById('edit-prod-id').value, name = document.getElementById('edit-prod-name').value.trim(), price = document.getElementById('edit-prod-price').value, stock = document.getElementById('edit-prod-stock').value;
                if(!name || !price || !stock) return showToast("အချက်အလက် ပြည့်စုံစွာ ဖြည့်ပါ။"); tg.MainButton.showProgress();
                const res = await apiFetch(`/api/vendor/products/${id}`, { method: 'PUT', body: JSON.stringify({ name, price, stock }) }); tg.MainButton.hideProgress();
                if(res.ok) { showToast("✅ ပြင်ဆင်ပြီးပါပြီ"); closeEditModal(); loadVendorProducts(); loadProducts(); } else { showToast("⚠️ အမှားအယွင်းဖြစ်ပေါ်ခဲ့ပါသည်။"); }
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
