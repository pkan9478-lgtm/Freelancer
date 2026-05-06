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
app = FastAPI(title="Digital Mall Auto-Run System Pro (Storefront Enhanced)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_headers=["*"], allow_methods=["*"])

try:
    if REDIS_URL:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        print("✅ Redis Connected")
except: 
    print("⚠️ Redis Not Connected - Proceeding with memory-optimized mode")
    redis_client = None

# ==========================================
# ၂။ DATABASE MODELS (Optimized for Low RAM)
# ==========================================
DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR}/mall_ai_pro_final.db")

# Added timeout to prevent SQLite locks on low-tier servers
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 15} if "sqlite" in DATABASE_URL else {})
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
    
    # Store / Local Commerce Settings
    store_name = Column(String, default="")
    store_description = Column(String, default="")
    store_state = Column(String, default="")
    store_district = Column(String, default="")
    store_township = Column(String, default="")
    store_ward = Column(String, default="")
    store_street = Column(String, default="")
    store_cover = Column(Text, default="") 

    # Vendor Payment Profile Settings
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
    category = Column(String, default="General", index=True) 
    custom_category = Column(String, default="General") 
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
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    message = Column(String)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

Base.metadata.create_all(bind=engine)

# Auto-Migration
try:
    with engine.begin() as conn: conn.execute(text("ALTER TABLE users ADD COLUMN store_cover TEXT DEFAULT ''"))
except Exception: pass
try:
    with engine.begin() as conn: conn.execute(text("ALTER TABLE products ADD COLUMN custom_category TEXT DEFAULT 'General'"))
except Exception: pass
try:
    with engine.begin() as conn: conn.execute(text("ALTER TABLE orders ADD COLUMN payment_slip TEXT DEFAULT ''"))
except Exception: pass
try:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN store_name TEXT DEFAULT ''"))
        conn.execute(text("ALTER TABLE users ADD COLUMN store_description TEXT DEFAULT ''"))
        conn.execute(text("ALTER TABLE users ADD COLUMN store_state TEXT DEFAULT ''"))
        conn.execute(text("ALTER TABLE users ADD COLUMN store_district TEXT DEFAULT ''"))
        conn.execute(text("ALTER TABLE users ADD COLUMN store_township TEXT DEFAULT ''"))
        conn.execute(text("ALTER TABLE users ADD COLUMN store_ward TEXT DEFAULT ''"))
        conn.execute(text("ALTER TABLE users ADD COLUMN store_street TEXT DEFAULT ''"))
except Exception: pass

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# ==========================================
# ၃။ SECURE AUTHENTICATION (Telegram WebApp App-Free)
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
        res = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}", timeout=10)
        return Response(content=res.content, media_type="image/jpeg")
    except: raise HTTPException(status_code=404)

# ==========================================
# ၄။ API ENDPOINTS
# ==========================================
@app.get("/api/auth")
def authenticate_user(user: User = Depends(get_current_user)):
    has_payment = user.accept_cod or user.kpay_phone or user.wave_phone or user.kpay_qr or user.wave_qr
    has_store = bool(user.store_name and user.store_state and user.store_ward and user.store_street)
    vendor_ready = has_payment and has_store
    
    return {
        "user": {
            "id": user.telegram_id, "name": user.full_name, "role": user.role, 
            "default_address": user.default_address, "phone": user.phone,
            "vendor_ready": vendor_ready,
            "store_name": user.store_name, "store_state": user.store_state, 
            "store_district": user.store_district, "store_township": user.store_township,
            "store_ward": user.store_ward, "store_street": user.store_street,
            "store_cover": user.store_cover,
            "kpay_phone": user.kpay_phone, "wave_phone": user.wave_phone,
            "accept_cod": user.accept_cod
        }
    }

@app.get("/api/locations")
def get_locations():
    return {
        "ရန်ကုန်တိုင်းဒေသကြီး": {"ရန်ကုန်အနောက်ပိုင်းခရိုင်": ["ကမာရွတ်", "လှိုင်", "စမ်းချောင်း", "အလုံ", "ကြည့်မြင်တိုင်", "ဒဂုံ", "ဗဟန်း", "ကျောက်တံတား", "ပန်းဘဲတန်း", "လသာ", "လမ်းမတော်"], "ရန်ကုန်အရှေ့ပိုင်းခရိုင်": ["သင်္ဃန်းကျွန်း", "ရန်ကင်း", "တောင်ဥက္ကလာပ", "မြောက်ဥက္ကလာပ", "သာကေတ", "ဒေါပုံ", "တာမွေ", "ပုဇွန်တောင်", "ဗိုလ်တထောင်", "ဒဂုံမြို့သစ်(တောင်ပိုင်း)", "ဒဂုံမြို့သစ်(မြောက်ပိုင်း)", "ဒဂုံမြို့သစ်(အရှေ့ပိုင်း)", "ဒဂုံမြို့သစ်(ဆိပ်ကမ်း)"], "ရန်ကုန်မြောက်ပိုင်းခရိုင်": ["အင်းစိန်", "မင်္ဂလာဒုံ", "မှော်ဘီ", "လှည်းကူး", "တိုက်ကြီး", "ထန်းတပင်", "ရွှေပြည်သာ", "လှိုင်သာယာ"], "ရန်ကုန်တောင်ပိုင်းခရိုင်": ["သန်လျင်", "ကျောက်တန်း", "ခရမ်း", "သုံးခွ", "တွံတေး", "ကော့မှူး", "ကွမ်းခြံကုန်း", "ဒလ", "ဆိပ်ကြီးခနောင်တို"]},
        "မန္တလေးတိုင်းဒေသကြီး": {"မန္တလေးခရိုင်": ["အောင်မြေသာစံ", "ချမ်းအေးသာစံ", "မဟာအောင်မြေ", "ချမ်းမြသာစည်", "ပြည်ကြီးတံခွန်", "အမရပူရ", "ပုသိမ်ကြီး"], "ပြင်ဦးလွင်ခရိုင်": ["ပြင်ဦးလွင်", "မတ္တရာ", "စဉ့်ကူး", "မိုးကုတ်", "သပိတ်ကျင်း"], "ကျောက်ဆည်ခရိုင်": ["ကျောက်ဆည်", "စဉ့်ကိုင်", "မြစ်သား", "တံတားဦး"], "မိတ္ထီလာခရိုင်": ["မိတ္ထီလာ", "မလှိုင်", "သာစည်", "ဝမ်းတွင်း"], "မြင်းခြံခရိုင်": ["မြင်းခြံ", "တောင်သာ", "နွားထိုးကြီး", "ကျောက်ပန်းတောင်း", "ငါန်းဇွန်"], "ညောင်ဦးခရိုင်": ["ညောင်ဦး", "ကျောက်ပန်းတောင်း"], "ရမည်းသင်းခရိုင်": ["ရမည်းသင်း", "ပျော်ဘွယ်"]},
        "နေပြည်တော်": {"ဥတ္တရခရိုင်": ["ဥတ္တရသီရိ", "ပုဗ္ဗသီရိ", "ဇေယျာသီရိ", "တပ်ကုန်း"], "ဒက္ခိဏခရိုင်": ["ဒက္ခိဏသီရိ", "ဇမ္ဗူသီရိ", "ပျဉ်းမနား", "လယ်ဝေး"]},
        "ပဲခူးတိုင်းဒေသကြီး": {"ပဲခူးခရိုင်": ["ပဲခူး", "ဒိုက်ဦး", "ကဝ", "သနပ်ပင်", "ဝေါ", "ညောင်လေးပင်", "ကျောက်တံခါး", "ရွှေကျင်"], "တောင်ငူခရိုင်": ["တောင်ငူ", "ရေတာရှည်", "ကျောက်ကြီး", "ဖြူး", "အုတ်တွင်း", "ထန်းတပင်"], "ပြည်ခရိုင်": ["ပြည်", "ပေါက်ခေါင်း", "ပန်းတောင်း", "ပေါင်းတည်", "သဲကုန်း", "ရွှေတောင်"], "သာယာဝတီခရိုင်": ["သာယာဝတီ", "လက်ပံတန်း", "မင်းလှ", "မိုးညို", "အုတ်ဖို", "ကြို့ပင်ကောက်", "ဇီးကုန်း", "နတ်တလင်း"]},
        "ဧရာဝတီတိုင်းဒေသကြီး": {"ပုသိမ်ခရိုင်": ["ပုသိမ်", "ကန်ကြီးထောင့်", "သာပေါင်း", "ငပုတော", "ကျုံပျော်", "ရေကြည်", "ကျောင်းကုန်း"], "ဟင်္သာတခရိုင်": ["ဟင်္သာတ", "ဇလွန်", "လေးမျက်နှာ", "မြန်အောင်", "ကြံခင်း", "အင်္ဂပူ"], "မြောင်းမြခရိုင်": ["မြောင်းမြ", "အိမ်မဲ", "ဝါးခယ်မ"], "မအူပင်ခရိုင်": ["မအူပင်", "ပန်းတနော်", "ညောင်တုန်း", "ဓနုဖြူ"], "ဖျာပုံခရိုင်": ["ဖျာပုံ", "ဘိုကလေး", "ကျိုက်လတ်", "ဒေးဒရဲ"], "လပွတ္တာခရိုင်": ["လပွတ္တာ", "မော်လမြိုင်ကျွန်း"]},
        "မွန်ပြည်နယ်": {"မော်လမြိုင်ခရိုင်": ["မော်လမြိုင်", "ကျိုက်မရော", "ချောင်းဆုံ", "သံဖြူဇရပ်", "မုဒုံ", "ရေး"], "သထုံခရိုင်": ["သထုံ", "ပေါင်", "ကျိုက်ထို", "ဘီးလင်း"]},
        "ရှမ်းပြည်နယ်": {"တောင်ကြီးခရိုင်": ["တောင်ကြီး", "ညောင်ရွှေ", "ဟိုပုံး", "ဆီဆိုင်", "ကလော", "ပင်းတယ", "ရွာငံ", "ရပ်စောက်"], "လားရှိုးခရိုင်": ["လားရှိုး", "သိန္နီ", "မိုင်းရယ်", "တန့်ယန်း"], "ကျိုင်းတုံခရိုင်": ["ကျိုင်းတုံ", "မိုင်းခတ်", "မိုင်းပြင်း", "မိုင်းယန်း"], "တာချီလိတ်ခရိုင်": ["တာချီလိတ်", "မိုင်းဖြတ်", "မိုင်းယောင်း"], "မူဆယ်ခရိုင်": ["မူဆယ်", "နမ့်ခမ်း", "ကွတ်ခိုင်"]},
        "စစ်ကိုင်းတိုင်းဒေသကြီး": {"စစ်ကိုင်းခရိုင်": ["စစ်ကိုင်း", "မြင်းမူ", "မြောင်"], "မုံရွာခရိုင်": ["မုံရွာ", "အရာတော်", "ချောင်းဦး", "ဘုတလင်"], "ရွှေဘိုခရိုင်": ["ရွှေဘို", "ခင်ဦး", "ဝက်လက်", "ကန့်ဘလူ", "ကျွန်းလှ", "ရေဦး", "ဒီပဲယင်း", "တန့်ဆည်"], "ကလေးခရိုင်": ["ကလေး", "ကလေးဝ", "မင်းကင်း"]},
        "မကွေးတိုင်းဒေသကြီး": {"မကွေးခရိုင်": ["မကွေး", "ရေနံချောင်း", "ချောက်", "တောင်တွင်းကြီး", "မြို့သစ်", "နတ်မောက်"], "မင်းဘူးခရိုင်": ["မင်းဘူး", "ပွင့်ဖြူ", "ငဖဲ", "စေတုတ္တရာ"], "ပခုက္ကူခရိုင်": ["ပခုက္ကူ", "ရေစကြို", "မြိုင်", "ပေါက်", "ဆိပ်ဖြူ"], "သရက်ခရိုင်": ["သရက်", "မင်းတုန်း", "မင်းလှ", "အောင်လံ", "ကံမ", "ဆင်ပေါင်ဝဲ"]},
        "ကရင်ပြည်နယ်": {"ဘားအံခရိုင်": ["ဘားအံ", "လှိုင်းဘွဲ", "ဖာပွန်", "သံတောင်ကြီး"], "မြဝတီခရိုင်": ["မြဝတီ"], "ကော့ကရိတ်ခရိုင်": ["ကော့ကရိတ်", "ကြာအင်းဆိပ်ကြီး"]},
        "ကယားပြည်နယ်": {"လွိုင်ကော်ခရိုင်": ["လွိုင်ကော်", "ဒီမော့ဆို", "ဖရူဆို", "ရှားတော"], "ဘောလခဲခရိုင်": ["ဘောလခဲ", "ဖားဆောင်း", "မယ်စဲ့"]},
        "ကချင်ပြည်နယ်": {"မြစ်ကြီးနားခရိုင်": ["မြစ်ကြီးနား", "ဝိုင်းမော်", "အင်ဂျန်းယန်", "တနိုင်း", "ချီဖွေ", "ဆော့လော်"], "ဗန်းမော်ခရိုင်": ["ဗန်းမော်", "ရွှေကူ", "မိုးမောက်", "မန်စီ"], "မိုးညှင်းခရိုင်": ["မိုးညှင်း", "မိုးကောင်း", "ဖားကန့်"]},
        "ချင်းပြည်နယ်": {"ဟားခါးခရိုင်": ["ဟားခါး", "ထန်တလန်"], "ဖလမ်းခရိုင်": ["ဖလမ်း", "တီတိန်", "တွန်းဇံ"], "မင်းတပ်ခရိုင်": ["မင်းတပ်", "မတူပီ", "ကန်ပက်လက်", "ပလက်ဝ"]},
        "ရခိုင်ပြည်နယ်": {"စစ်တွေခရိုင်": ["စစ်တွေ", "ပုဏ္ဏားကျွန်း", "မြောက်ဦး", "ကျောက်တော်", "မင်းပြား", "မြေပုံ", "ပေါက်တော", "ရသေ့တောင်"], "မောင်တောခရိုင်": ["မောင်တော", "ဘူးသီးတောင်"], "ကျောက်ဖြူခရိုင်": ["ကျောက်ဖြူ", "မာန်အောင်", "ရမ်းဗြဲ", "အမ်း"], "သံတွဲခရိုင်": ["သံတွဲ", "တောင်ကုတ်", "ဂွ"]},
        "တနင်္သာရီတိုင်းဒေသကြီး": {"ထားဝယ်ခရိုင်": ["ထားဝယ်", "လောင်းလုံး", "သရက်ချောင်း", "ရေဖြူ"], "မြိတ်ခရိုင်": ["မြိတ်", "ကျွန်းစု", "ပုလော", "တနင်္သာရီ"], "ကော့သောင်းခရိုင်": ["ကော့သောင်း", "ဘုတ်ပြင်း"]}
    }

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
    
    if "store_name" in data: user.store_name = data.get("store_name", user.store_name)
    if "store_state" in data: user.store_state = data.get("store_state", user.store_state)
    if "store_district" in data: user.store_district = data.get("store_district", user.store_district)
    if "store_township" in data: user.store_township = data.get("store_township", user.store_township)
    if "store_ward" in data: user.store_ward = data.get("store_ward", user.store_ward)
    if "store_street" in data: user.store_street = data.get("store_street", user.store_street)
    if "store_cover" in data: user.store_cover = data.get("store_cover", user.store_cover)

    if "accept_cod" in data: user.accept_cod = data.get("accept_cod", user.accept_cod)
    if "kpay_phone" in data: user.kpay_phone = data.get("kpay_phone", user.kpay_phone)
    if "wave_phone" in data: user.wave_phone = data.get("wave_phone", user.wave_phone)
    if "kpay_qr" in data: user.kpay_qr = data.get("kpay_qr")
    if "wave_qr" in data: user.wave_qr = data.get("wave_qr")
    
    db.commit()
    return {"status": "success"}

@app.get("/api/products")
def get_products(category: str = "All", search: str = "", state: str = "All", township: str = "All", ward: str = "", skip: int = 0, limit: int = 20, db: Session = Depends(get_db)):
    query = db.query(Product).join(User)
    
    if category != "All": query = query.filter(Product.category == category)
    if search: query = query.filter(Product.name.ilike(f"%{search}%"))
    if state != "All": query = query.filter(User.store_state == state)
    if township != "All" and township != "": query = query.filter(User.store_township == township)
    if ward: query = query.filter(User.store_ward.ilike(f"%{ward}%"))

    products = query.order_by(Product.id.desc()).offset(skip).limit(limit).all()
    categories = [c[0] for c in db.query(Product.category).distinct().all()] 
    states = [s[0] for s in db.query(User.store_state).filter(User.store_state != "").distinct().all()]
    
    res = [{
        "id": p.id, "name": p.name, "price": p.price, "desc": p.description, "category": p.category, 
        "custom_category": p.custom_category,
        "img": p.image_file_id, "stock": p.stock, 
        "vendor_id": p.vendor_id, 
        "vendor_name": p.vendor.store_name if p.vendor.store_name else p.vendor.full_name,
        "vendor_state": p.vendor.store_state,
        "vendor_cod": p.vendor.accept_cod, "vendor_kpay": p.vendor.kpay_phone, 
        "vendor_wave": p.vendor.wave_phone, "kpay_qr": p.vendor.kpay_qr, "wave_qr": p.vendor.wave_qr
    } for p in products]
    
    return {"products": res, "categories": categories, "states": states}

@app.get("/api/store/{vendor_id}")
def get_store(vendor_id: int, db: Session = Depends(get_db)):
    vendor = db.query(User).filter(User.id == vendor_id).first()
    if not vendor: raise HTTPException(status_code=404)
    products = db.query(Product).filter(Product.vendor_id == vendor_id).order_by(Product.id.desc()).all()
    
    res_prods = [{
        "id": p.id, "name": p.name, "price": p.price, "desc": p.description, 
        "category": p.category, "custom_category": p.custom_category,
        "img": p.image_file_id, "stock": p.stock, 
        "vendor_id": p.vendor_id, "vendor_name": vendor.store_name if vendor.store_name else vendor.full_name,
        "vendor_state": vendor.store_state, "vendor_cod": vendor.accept_cod, "vendor_kpay": vendor.kpay_phone, 
        "vendor_wave": vendor.wave_phone, "kpay_qr": vendor.kpay_qr, "wave_qr": vendor.wave_qr
    } for p in products]
    
    store_categories = list(set([p.custom_category for p in products if p.custom_category]))
    if not store_categories: store_categories = ["General"]
    
    return {
        "store": {
            "name": vendor.store_name if vendor.store_name else vendor.full_name,
            "state": vendor.store_state,
            "district": vendor.store_district,
            "township": vendor.store_township,
            "ward": vendor.store_ward,
            "street": vendor.store_street,
            "phone": vendor.phone,
            "cover": vendor.store_cover
        },
        "categories": store_categories,
        "products": res_prods
    }

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
            
            bot.send_message(user.telegram_id, f"🛒 **အော်ဒါ လက်ခံရရှိပါသည်**\n\n{items_str}\n\nစုစုပေါင်း: {total_amount:,.0f} Ks\nလိပ်စာ: {address}\nငွေချေစနစ်: {pay_msg}\n\n_ရောင်းချသူမှ အတည်ပြုပြီးပါက ဆက်လက်အကြောင်းကြားပေးပါမည်။_", parse_mode="Markdown")
            
            if vendor_notify:
                vendor_caption = f"🔔 **အော်ဒါအသစ်ဝင်ပါသည်**\nဝယ်သူ: {user.full_name} (Ph: {phone})\n{items_str}\nလိပ်စာ: {address}\nငွေချေစနစ်: {pay_msg}\n\nApp ထဲတွင် ငွေလွှဲပြေစာနှင့် အချက်အလက်များကို သေချာစစ်ဆေး၍ အတည်ပြုပေးပါ။"
                
                if payment_slip_b64 and payment_method != "COD":
                    try:
                        img_data = base64.b64decode(payment_slip_b64.split(',')[1] if ',' in payment_slip_b64 else payment_slip_b64)
                        bot.send_photo(vendor_notify, photo=img_data, caption=vendor_caption, parse_mode="Markdown")
                    except Exception:
                        bot.send_message(vendor_notify, vendor_caption + "\n_(ငွေလွှဲပြေစာပုံအား App ထဲရှိ စီမံရန် -> အော်ဒါများ တွင် ဝင်ကြည့်ပါ။)_", parse_mode="Markdown")
                else:
                    bot.send_message(vendor_notify, vendor_caption, parse_mode="Markdown")
        except Exception as e: print("Telegram Send Error:", e)

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
    status_map = {
        "pending": ("⏳ အော်ဒါကို စစ်ဆေးနေဆဲဖြစ်ပါသည်။", "စစ်ဆေးဆဲ"),
        "approved": ("✅ အော်ဒါ အတည်ပြုပါသည်။ ထုပ်ပိုးနေပါသည်။", "ထုပ်ပိုးနေသည်"), 
        "shipped": ("🚚 ပစ္စည်းပို့ဆောင်ပေးလိုက်ပါပြီ။", "ပို့ဆောင်နေသည်"), 
        "delivered": ("🎁 ပစ္စည်းလက်ခံရရှိကြောင်း မှတ်တမ်းတင်ပြီးပါပြီ။", "ရောက်ရှိပါပြီ"), 
        "cancelled": ("❌ သင့်အော်ဒါအား ပယ်ဖျက်လိုက်ပါသည်။", "ပယ်ဖျက်လိုက်သည်")
    }
    
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or (order.product.vendor_id != user.id and user.role != "admin"): raise HTTPException(status_code=400, detail="Permission Denied")
    
    new_status = request.query_params.get("status")
    if new_status not in status_map: raise HTTPException(status_code=400, detail="Invalid Status")
    
    if new_status == "cancelled" and order.status != "cancelled": order.product.stock += order.quantity 
    elif order.status == "cancelled" and new_status != "cancelled": order.product.stock -= order.quantity

    order.status = new_status
    
    short_status = status_map[new_status][1]
    noti_msg = f"သင့်အော်ဒါ '{order.product.name}' ၏ အခြေအနေမှာ '{short_status}' သို့ ပြောင်းလဲသွားပါသည်။"
    db.add(Notification(user_id=order.user_id, message=noti_msg))
    db.commit()

    try: bot.send_message(order.user.telegram_id, f"{status_map[new_status][0]}\nပစ္စည်း: **{order.product.name} (x{order.quantity})**", parse_mode="Markdown")
    except: pass
    
    return {"status": "success"}

@app.get("/api/vendor/products")
def get_vendor_products(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["vendor", "admin"]: raise HTTPException(status_code=403)
    products = db.query(Product).filter(Product.vendor_id == user.id).order_by(Product.id.desc()).all()
    categories = list(set([p.custom_category for p in products if p.custom_category]))
    return {
        "products": [{"id":p.id, "name":p.name, "price":p.price, "stock":p.stock, "custom_category": p.custom_category, "img":p.image_file_id} for p in products],
        "categories": categories
    }

@app.put("/api/vendor/products/{product_id}")
async def edit_product(product_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = await request.json()
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user.id).first()
    if not product: raise HTTPException(status_code=404, detail="ပစ္စည်းမရှိပါ")
    
    if "name" in data: product.name = data.get("name", product.name)
    if "price" in data: product.price = float(data.get("price", product.price))
    if "stock" in data: product.stock = int(data.get("stock", product.stock))
    if "custom_category" in data: product.custom_category = data.get("custom_category", product.custom_category)
    db.commit()
    return {"status": "success"}

@app.delete("/api/vendor/products/{product_id}")
def delete_product(product_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user.id).first()
    if product: db.delete(product); db.commit()
    return {"status": "success"}

# ==========================================
# ၅။ AI-POWERED CMS CHAT BOT (Groq Rate Limit Protected)
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

    has_payment = user.accept_cod or user.kpay_phone or user.wave_phone or user.kpay_qr
    has_store = bool(user.store_name and user.store_state and user.store_ward)
    
    if not has_payment or not has_store:
        bot.reply_to(message, "⚠️ **ရောင်းချရန် ဆိုင်အချက်အလက် (သို့) ငွေပေးချေမှုစနစ် မသတ်မှတ်ရသေးပါ။**\nApp ထဲသို့ဝင်၍ 'စီမံရန် -> Profile Setting' တွင် ဆိုင်အမည်၊ အသေးစိတ်လိပ်စာ နှင့် KPay/COD စနစ်တို့ကို အရင်သတ်မှတ်ပေးပါ။", parse_mode="Markdown")
        return db.close()

    if user.role == "buyer":
        user.role = "vendor"
        db.commit()

    try:
        caption = message.caption or "New Product"
        file_id = message.photo[-1].file_id 
        
        # Default configuration in case Groq limit is hit
        ai_data = {"name": caption[:30] + "..." if len(caption) > 30 else caption, "price": 0, "category": "General", "description": caption, "stock": 10}
        
        if GROQ_API_KEY:
            try:
                msg = bot.reply_to(message, "⏳ AI ဖြင့် ပစ္စည်းအချက်အလက် ခွဲခြမ်းစိတ်ဖြာနေပါသည်...")
                headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
                prompt = f"""Analyze the Burmese text for an e-commerce product: "{caption}". Extract details to strictly JSON. Required keys: 'name', 'price' (numeric), 'category', 'description', 'stock' (numeric)."""
                
                # Model changed to mixtral-8x7b-32768 for better token efficiency and speed
                payload = {
                    "model": "mixtral-8x7b-32768", 
                    "messages": [{"role": "user", "content": prompt}], 
                    "response_format": {"type": "json_object"}
                }
                
                # Added timeout and status check to protect against Groq Limits
                res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=15)
                
                if res.status_code == 200:
                    res_json = res.json()
                    parsed = json.loads(res_json['choices'][0]['message']['content'])
                    for k in ['name', 'price', 'category', 'description', 'stock']:
                        if parsed.get(k): ai_data[k] = parsed[k]
                elif res.status_code == 429:
                    print("⚠️ Groq API Rate Limit Hit - Falling back to default parser to maintain system stability.")
                else:
                    print(f"⚠️ Groq API Error: {res.status_code}")
                    
                bot.delete_message(message.chat.id, msg.message_id)
            except Exception as e:
                print(f"Groq Request Exception: {e}")
                # System continues gracefully using default ai_data

        new_product = Product(name=ai_data['name'], price=float(ai_data['price']), description=ai_data['description'], category=ai_data['category'], custom_category="General", stock=int(ai_data['stock']), image_file_id=file_id, vendor_id=user.id)
        db.add(new_product)
        db.commit()
        bot.reply_to(message, f"✅ **ပစ္စည်း အလိုအလျောက် တင်ပြီးပါပြီ။**\n\n📌 {ai_data['name']}\n💰 {ai_data['price']} Ks\n📦 Stock: {ai_data['stock']}", parse_mode="Markdown")
    except Exception as e: 
        print(e)
        bot.reply_to(message, f"အမှားအယွင်း ဖြစ်ပေါ်ခဲ့ပါသည်။")
    finally: db.close()

# ==========================================
# ၆။ FRONTEND UI (Fully Preserved & Untouched)
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
            @keyframes slideInRight { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
            .animate-slide-in { animation: slideInRight 0.3s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
            @keyframes slideOutRight { from { transform: translateX(0); opacity: 1; } to { transform: translateX(100%); opacity: 0; } }
            .animate-slide-out { animation: slideOutRight 0.3s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
            @keyframes float { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-5px); } }
            .animate-float { animation: float 3s ease-in-out infinite; }
            @keyframes bounceShort { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.2); } }
            .animate-bounce-short { animation: bounceShort 0.3s ease-out; }

            .gradient-text { background: linear-gradient(135deg, #2563eb, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .gradient-bg { background: linear-gradient(135deg, #2563eb, #8b5cf6); }
            
            /* High 5D Drop Shadow Effect for Product Cards */
            .shadow-5d { box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.3), 0 15px 25px -15px rgba(0, 0, 0, 0.15); }
            
            .glass-header { background: rgba(255, 255, 255, 0.85); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border-bottom: 1px solid rgba(226, 232, 240, 0.8); }
            .glass-bottom-nav { background: rgba(255, 255, 255, 0.90); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border-top: 1px solid rgba(226, 232, 240, 0.8); }
            .btn-press:active { transform: scale(0.95); transition: transform 0.1s ease; }
            .tab-btn { color: #64748b; transition: all 0.2s ease; }
            .tab-btn.active { color: #4f46e5; }
            .cat-chip { transition: all 0.2s ease; border: 1px solid #e2e8f0; }
            .cat-chip.active { background: linear-gradient(135deg, #2563eb, #8b5cf6); color: white; border-color: transparent; box-shadow: 0 4px 6px -1px rgba(99, 102, 241, 0.2); }
            .badge { position: absolute; top: -3px; right: -3px; background: #ef4444; color: white; border-radius: 50%; padding: 2px 6px; font-size: 10px; font-weight: 800; box-shadow: 0 2px 4px rgba(239,68,68,0.3); }
            
            .modal-overlay { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.8); backdrop-filter: blur(6px); z-index: 60; display: none; align-items: center; justify-content: center; padding: 20px; }
            .modal-overlay.active { display: flex; animation: fadeIn 0.2s ease-out; }
            .slide-up-modal { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.5); z-index: 70; display: none; flex-direction: column; justify-content: flex-end; }
            .slide-up-modal.active { display: flex; animation: fadeIn 0.2s; }
            .slide-up-content { background: white; border-radius: 24px 24px 0 0; padding: 24px; max-height: 80vh; overflow-y: auto; animation: slideUp 0.3s cubic-bezier(0.16, 1, 0.3, 1); }
            @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }
            
            #storefront-view { position: fixed; inset: 0; background: #f8fafc; z-index: 55; display: none; overflow-y: auto; padding-bottom: 90px; }
            #storefront-view.active { display: block; animation: slideInRight 0.3s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
            #storefront-view.closing { animation: slideOutRight 0.3s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
            
            .store-header-fixed { position: sticky; top: 0; z-index: 40; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }
            .store-cover-container { width: 100%; aspect-ratio: 16/9; background-size: cover; background-position: center; position: relative; }
            .store-cover-overlay { position: absolute; inset: 0; background: linear-gradient(to top, rgba(15,23,42,0.95) 0%, rgba(15,23,42,0.4) 40%, rgba(15,23,42,0.1) 100%); }

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
            .status-cancelled { background-color: #fef2f2; color: #ef4444; padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 800; border: 1px solid #fee2e2; }
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
                <button onclick="showTab('cart-tab', 'btn-shop')" class="btn-press relative p-2.5 rounded-full bg-indigo-50 text-indigo-600 hover:bg-indigo-100 transition">
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
                <div class="absolute -top-5 gradient-bg text-white w-14 h-14 rounded-full flex items-center justify-center shadow-[0_8px_16px_rgba(99,102,241,0.3)] border-4 border-[#f8fafc] text-3xl pb-1 animate-float z-10">+</div>
                <span class="mt-7 font-extrabold gradient-text">ရောင်းမည်</span>
            </button>
            <button id="btn-history" onclick="showTab('history-tab', 'btn-history')" class="tab-btn btn-press flex-1 py-3 flex flex-col items-center gap-1.5">
                <span class="text-[22px] leading-none">📋</span><span>မှတ်တမ်း</span>
            </button>
            <button id="btn-orders" onclick="showTab('orders-tab', 'btn-orders')" class="tab-btn btn-press hidden flex-1 py-3 flex flex-col items-center gap-1.5">
                <span class="text-[22px] leading-none">⚙️</span><span>စီမံရန်</span>
            </button>
        </div>

        <div id="storefront-view">
            <div class="store-header-fixed bg-white">
                <div id="storefront-cover-img" class="store-cover-container" style="background-image: url('https://via.placeholder.com/800x450');">
                    <div class="store-cover-overlay"></div>
                    <button onclick="closeStore()" class="absolute top-4 left-4 text-white bg-white/20 hover:bg-white/40 backdrop-blur rounded-full w-10 h-10 flex items-center justify-center font-bold btn-press transition z-20 text-xl shadow-sm">&larr;</button>
                    <div class="absolute bottom-4 left-5 right-5 text-white z-20">
                        <div class="flex items-center gap-3 mb-2">
                            <div class="w-12 h-12 bg-white rounded-xl shadow-lg border-2 border-white/30 flex items-center justify-center text-2xl shrink-0 text-black">🏪</div>
                            <div>
                                <h2 id="storefront-name" class="text-xl font-black mb-0.5 drop-shadow-md leading-tight">ဆိုင်အမည်</h2>
                                <div class="text-[11px] font-bold text-slate-200 drop-shadow flex items-start gap-1"><span class="mt-0.5">📍</span> <span id="storefront-location" class="line-clamp-1">တည်နေရာ</span></div>
                            </div>
                        </div>
                    </div>
                </div>
                <div id="storefront-categories" class="px-4 py-3 flex gap-2.5 overflow-x-auto scrollbar-hide border-b border-slate-100 bg-white"></div>
            </div>
            
            <div class="p-5">
                <div class="flex justify-between items-end mb-4">
                    <h3 id="storefront-cat-title" class="font-extrabold text-slate-800 text-lg">အားလုံး</h3>
                    <span id="storefront-count" class="text-xs font-bold text-indigo-600 bg-indigo-50 px-2 py-1 rounded-lg border border-indigo-100">0 Items</span>
                </div>
                <div id="storefront-products" class="grid grid-cols-2 gap-4 pb-10"></div>
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
                <button onclick="closeSlipModal()" class="absolute -top-12 right-0 text-white bg-white/20 hover:bg-white/40 backdrop-blur rounded-full w-10 h-10 flex items-center justify-center font-bold text-2xl btn-press transition">&times;</button>
                <div class="bg-white p-2 rounded-2xl shadow-2xl w-full">
                    <h3 class="font-extrabold text-center text-slate-800 py-3 border-b border-slate-100">💳 ဝယ်သူတင်ထားသော ငွေလွှဲပြေစာ</h3>
                    <img id="slip-viewer-img" class="w-full h-auto max-h-[65vh] object-contain rounded-xl mt-2 bg-slate-50">
                </div>
            </div>
        </div>

        <div id="edit-product-modal" class="modal-overlay">
            <div class="bg-white w-full max-w-sm rounded-[24px] p-6 shadow-2xl relative animate-fade-up">
                <button onclick="closeEditModal()" class="absolute top-4 right-4 text-slate-400 bg-slate-100 rounded-full w-8 h-8 flex items-center justify-center font-bold text-xl btn-press">&times;</button>
                <h2 class="font-extrabold text-lg mb-5 text-slate-800">ပစ္စည်း အချက်အလက် ပြင်မည်</h2>
                <input type="hidden" id="edit-prod-id">
                <div class="space-y-4">
                    <div>
                        <label class="block text-xs font-bold text-slate-500 mb-1.5">ပစ္စည်း အမည်</label>
                        <input type="text" id="edit-prod-name" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition">
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-500 mb-1.5">ဈေးနှုန်း (Ks)</label>
                        <input type="number" id="edit-prod-price" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition">
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-500 mb-1.5">လက်ကျန် (Stock)</label>
                        <input type="number" id="edit-prod-stock" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition">
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-500 mb-1.5">ဆိုင်ပြခန်း အမျိုးအစား (Menu Option)</label>
                        <input type="text" id="edit-prod-cat" list="vendor-categories-list" placeholder="ဥပမာ - အင်္ကျီများ, ဖိနပ်များ..." class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition">
                    </div>
                </div>
                <button onclick="saveEditProduct()" class="btn-press w-full gradient-bg text-white font-bold py-3.5 rounded-xl shadow-[0_4px_12px_rgba(99,102,241,0.3)] mt-6 text-sm">သိမ်းဆည်းမည်</button>
            </div>
        </div>
        <datalist id="vendor-categories-list"></datalist>

        <div id="shop-tab" class="tab-content animate-fade-in">
            <div class="px-4 py-3 bg-indigo-50 border-b border-indigo-100 flex flex-col gap-2.5">
                <div class="text-[12px] font-extrabold text-indigo-900 flex items-center gap-1.5"><span class="text-base">📍</span> ဒေသတွင်း ဈေးကွက်ရှာဖွေရန်</div>
                <div class="flex gap-2">
                    <select id="local-state-filter" onchange="updateLocalTownships(this.value)" class="w-full bg-white border border-indigo-200 text-indigo-700 py-2.5 px-3 rounded-xl text-[13px] font-bold outline-none focus:ring-2 focus:ring-indigo-500 shadow-sm transition">
                        <option value="All">နေရာအားလုံး (တိုင်း/ပြည်နယ်)</option>
                    </select>
                </div>
                <div class="flex gap-2 hidden" id="advanced-location-filters">
                    <select id="local-township-filter" onchange="triggerSearch()" class="w-1/2 bg-white border border-indigo-200 text-indigo-700 py-2 px-3 rounded-xl text-[12px] font-bold outline-none focus:ring-2 focus:ring-indigo-500 shadow-sm transition">
                        <option value="All">မြို့နယ်အားလုံး</option>
                    </select>
                    <input type="text" id="local-ward-filter" oninput="triggerSearch()" placeholder="ရပ်ကွက် / ကျေးရွာ..." class="w-1/2 bg-white border border-indigo-200 text-slate-700 py-2 px-3 rounded-xl text-[12px] font-bold outline-none focus:ring-2 focus:ring-indigo-500 shadow-sm transition">
                </div>
            </div>

            <div class="p-4 bg-white shadow-sm border-b border-slate-100 mb-3 rounded-b-3xl">
                <div class="relative mb-4">
                    <span class="absolute left-4 top-3 text-slate-400">🔍</span>
                    <input type="text" id="search-box" oninput="triggerSearch()" placeholder="ရှာဖွေလိုသော ပစ္စည်းအမည်..." class="w-full py-3 pl-10 pr-4 bg-slate-50 rounded-2xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500 outline-none transition-all font-medium">
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
                <button onclick="switchVendorTab('dash')" id="v-tab-dash" class="btn-press flex-1 bg-white shadow-sm py-2.5 rounded-xl text-sm font-extrabold text-indigo-600 transition-all">အော်ဒါများ</button>
                <button onclick="switchVendorTab('prods')" id="v-tab-prods" class="btn-press flex-1 py-2.5 rounded-xl text-sm font-bold text-slate-500 transition-all">ပစ္စည်းများ</button>
                <button onclick="switchVendorTab('profile')" id="v-tab-profile" class="btn-press flex-1 py-2.5 rounded-xl text-sm font-bold text-slate-500 transition-all">Profile</button>
            </div>
            <div id="vendor-dash-view" class="animate-fade-up"><div id="order-list" class="space-y-4 pb-10"></div></div>
            
            <div id="vendor-prods-view" class="hidden animate-fade-up">
                <div class="bg-indigo-50 p-4 rounded-2xl mb-4 border border-indigo-100 shadow-sm">
                    <label class="block text-[11px] font-extrabold text-indigo-800 mb-2">🏷️ ပစ္စည်းအမျိုးအစား (Menu) အမြန်သတ်မှတ်ရန်</label>
                    <div class="flex gap-2">
                        <input type="text" id="quick-cat-input" list="vendor-categories-list" placeholder="အမျိုးအစား (Menu) အမည်ရွေးပါ..." class="w-full p-2.5 bg-white border border-indigo-200 rounded-xl text-[13px] font-bold outline-none focus:ring-2 focus:ring-indigo-500 transition">
                    </div>
                    <p class="text-[10px] text-indigo-600 mt-2 font-bold leading-relaxed">အထက်ပါအကွက်တွင် အမျိုးအစားအမည်ရေးပြီး အောက်ပါပစ္စည်းများရှိ <span class="bg-indigo-600 text-white px-1.5 py-0.5 rounded shadow-sm">Menu ပြောင်းမည်</span> ကိုနှိပ်ရုံဖြင့် တိုက်ရိုက်ရွှေ့ပြောင်းပေးပါမည်။</p>
                </div>
                <div id="vendor-product-list" class="space-y-3 pb-10"></div>
            </div>
            
            <div id="vendor-profile-view" class="hidden animate-fade-up">
                
                <div class="bg-white p-6 rounded-3xl shadow-sm border border-slate-100 pb-10 mb-5">
                    <h3 class="font-extrabold text-slate-800 text-lg mb-5 flex items-center gap-2">🏬 ဆိုင်အချက်အလက် (Address & Cover)</h3>
                    <div class="space-y-4">
                        <div class="bg-slate-50 p-4 rounded-2xl border border-slate-200">
                            <label class="block text-xs font-bold text-slate-700 mb-2">ဆိုင်အဖုံးပုံ (Cover Image 16:9)</label>
                            <input type="file" accept="image/*" onchange="compressAndEncodeImage(this, 'prof-store-cover', 1280)" class="text-xs text-slate-500 file:mr-4 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-xs file:font-bold file:bg-indigo-50 file:text-indigo-700 hover:file:bg-indigo-100 transition w-full">
                            <input type="hidden" id="prof-store-cover">
                            <img id="prof-store-cover-preview" class="w-full aspect-video object-cover rounded-xl hidden border border-slate-200 shadow-sm mt-3">
                        </div>

                        <div>
                            <label class="block text-xs font-bold text-slate-500 mb-1.5">ဆိုင်အမည် (Store Name) <span class="text-red-500">*</span></label>
                            <input type="text" id="prof-store-name" placeholder="ဥပမာ - Royal Fashion" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition">
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-500 mb-1.5">တိုင်းဒေသကြီး/ပြည်နယ် <span class="text-red-500">*</span></label>
                            <select id="prof-store-state" onchange="updateAddr('prof-store-district', mmData[this.value])" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition font-bold"></select>
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-500 mb-1.5">ခရိုင် <span class="text-red-500">*</span></label>
                            <select id="prof-store-district" onchange="updateAddr('prof-store-township', mmData[document.getElementById('prof-store-state').value][this.value])" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition font-bold"><option value="">-- ခရိုင် --</option></select>
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-500 mb-1.5">မြို့နယ် <span class="text-red-500">*</span></label>
                            <select id="prof-store-township" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition font-bold"><option value="">-- မြို့နယ် --</option></select>
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-500 mb-1.5">ရပ်ကွက် / ကျေးရွာ <span class="text-red-500">*</span></label>
                            <input type="text" id="prof-store-ward" placeholder="ဥပမာ - (က) ရပ်ကွက် သို့မဟုတ် ရွာမကျေးရွာ" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition">
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-500 mb-1.5">အိမ်အမှတ်၊ လမ်းအမည်၊ ပတ်ဝန်းကျင် <span class="text-red-500">*</span></label>
                            <input type="text" id="prof-store-street" placeholder="ဥပမာ - အမှတ်(၁)၊ ဗိုလ်ချုပ်လမ်း၊ ဈေးအနီး..." class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition">
                        </div>
                    </div>
                </div>

                <div class="bg-white p-6 rounded-3xl shadow-sm border border-slate-100 pb-10">
                    <h3 class="font-extrabold text-slate-800 text-lg mb-5 flex items-center gap-2">💳 ငွေပေးချေမှုစနစ်များ</h3>
                    <label class="flex items-center gap-3 p-4 bg-indigo-50/50 rounded-2xl border border-indigo-100 mb-5 cursor-pointer transition hover:bg-indigo-50">
                        <input type="checkbox" id="prof-cod" class="w-5 h-5 text-indigo-600 rounded border-gray-300 focus:ring-indigo-500">
                        <span class="font-bold text-sm text-indigo-900">အိမ်ရောက်မှ ငွေချေစနစ် (COD) ကို လက်ခံမည်</span>
                    </label>
                    <div class="space-y-5">
                        <div class="bg-slate-50 p-4 rounded-2xl border border-slate-200">
                            <label class="block text-xs font-extrabold text-indigo-700 mb-2">KPay ဖုန်း / QR</label>
                            <input type="text" id="prof-kpay-ph" placeholder="09xxxxxxxxx" class="w-full p-3 bg-white rounded-xl border border-slate-200 mb-3 text-sm outline-none focus:ring-2 focus:ring-indigo-500 transition">
                            <input type="file" accept="image/*" onchange="compressAndEncodeImage(this, 'prof-kpay-qr', 800)" class="text-xs text-slate-500 file:mr-4 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-xs file:font-bold file:bg-indigo-50 file:text-indigo-700 hover:file:bg-indigo-100 transition w-full">
                            <input type="hidden" id="prof-kpay-qr"><img id="prof-kpay-qr-preview" class="h-24 object-cover rounded-xl hidden border border-slate-200 shadow-sm mt-3">
                        </div>
                        <div class="bg-slate-50 p-4 rounded-2xl border border-slate-200">
                            <label class="block text-xs font-extrabold text-amber-600 mb-2">WavePay ဖုန်း / QR</label>
                            <input type="text" id="prof-wave-ph" placeholder="09xxxxxxxxx" class="w-full p-3 bg-white rounded-xl border border-slate-200 mb-3 text-sm outline-none focus:ring-2 focus:ring-amber-500 transition">
                            <input type="file" accept="image/*" onchange="compressAndEncodeImage(this, 'prof-wave-qr', 800)" class="text-xs text-slate-500 file:mr-4 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-xs file:font-bold file:bg-amber-50 file:text-amber-700 hover:file:bg-amber-100 transition w-full">
                            <input type="hidden" id="prof-wave-qr"><img id="prof-wave-qr-preview" class="h-24 object-cover rounded-xl hidden border border-slate-200 shadow-sm mt-3">
                        </div>
                    </div>
                    <button onclick="saveVendorProfile()" class="btn-press w-full mt-6 gradient-bg text-white font-bold py-3.5 rounded-xl shadow-[0_4px_12px_rgba(99,102,241,0.3)] text-sm">ဆိုင်အချက်အလက် သိမ်းမည်</button>
                </div>
            </div>
        </div>

        <div id="setup-modal" class="modal-overlay">
            <div class="bg-white w-full max-w-sm rounded-[24px] p-6 shadow-2xl relative animate-fade-up">
                <button onclick="document.getElementById('setup-modal').classList.remove('active')" class="btn-press absolute top-4 right-4 text-slate-400 bg-slate-100 rounded-full w-8 h-8 flex items-center justify-center font-bold text-xl">&times;</button>
                <div class="text-4xl mb-3 animate-bounce-short">🏪</div>
                <h2 class="font-extrabold text-xl mb-2 text-slate-800">ဆိုင်ဖွင့်ရန် လိုအပ်ချက်များ</h2>
                <p class="text-sm text-slate-500 mb-6 font-medium leading-relaxed">ပစ္စည်းမတင်မီ သင်၏ **ဆိုင်အမည်၊ လမ်း/ရပ်ကွက် အပါအဝင် အသေးစိတ်လိပ်စာ** နှင့် **ငွေချေစနစ် (KPay, Wave, COD)** များကို Profile တွင် အရင်သေချာစွာ သတ်မှတ်ပေးရန် လိုအပ်ပါသည်။</p>
                <button onclick="document.getElementById('setup-modal').classList.remove('active'); showTab('orders-tab', 'btn-orders'); switchVendorTab('profile');" class="btn-press w-full gradient-bg text-white font-bold py-3.5 rounded-xl shadow-md text-sm">Profile Setting သို့သွားရန်</button>
            </div>
        </div>

        <div id="toast">Message</div>

        <script>
            const tg = window.Telegram.WebApp;
            const initData = tg.initData; 
            let allProducts = [], currentCategory = 'All', cart = [];
            let searchTimeout = null, mmData = {}; 
            let currentUser = {};
            
            // Advanced Location Filters
            let localStateFilter = "All";
            let localTownshipFilter = "All";

            // Storefront variables
            let storeViewProducts = [];
            let storeCurrentCat = "All";

            function showToast(msg) {
                const t = document.getElementById("toast");
                t.innerText = msg; t.className = "show animate-bounce-short";
                if(tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
                setTimeout(() => { t.className = t.className.replace("show animate-bounce-short", ""); }, 3200);
            }

            async function apiFetch(url, options = {}) {
                return fetch(url, { ...options, headers: { 'X-Telegram-Init-Data': initData, 'Content-Type': 'application/json', ...options.headers }});
            }

            async function initApp() {
                tg.expand(); tg.ready();
                await fetchLocationData(); 
                fetchNotifications();
                
                try {
                    const res = await apiFetch('/api/auth'); const data = await res.json();
                    currentUser = data.user;
                    
                    document.getElementById('prof-cod').checked = (currentUser.accept_cod !== undefined) ? currentUser.accept_cod : true;
                    document.getElementById('prof-kpay-ph').value = currentUser.kpay_phone || '';
                    document.getElementById('prof-wave-ph').value = currentUser.wave_phone || '';
                    document.getElementById('prof-store-name').value = currentUser.store_name || '';
                    document.getElementById('prof-store-ward').value = currentUser.store_ward || '';
                    document.getElementById('prof-store-street').value = currentUser.store_street || '';
                    
                    if(currentUser.store_cover) {
                        document.getElementById('prof-store-cover').value = currentUser.store_cover;
                        let preview = document.getElementById('prof-store-cover-preview');
                        preview.src = currentUser.store_cover; preview.classList.remove('hidden');
                    }
                    
                    if(currentUser.store_state) {
                        document.getElementById('prof-store-state').value = currentUser.store_state;
                        updateAddr('prof-store-district', mmData[currentUser.store_state]);
                        setTimeout(() => {
                            if(currentUser.store_district) {
                                document.getElementById('prof-store-district').value = currentUser.store_district;
                                updateAddr('prof-store-township', mmData[currentUser.store_state][currentUser.store_district]);
                                setTimeout(() => {
                                    if(currentUser.store_township) document.getElementById('prof-store-township').value = currentUser.store_township;
                                }, 100);
                            }
                        }, 100);
                    }

                    if (currentUser.role === 'vendor' || currentUser.role === 'admin') { document.getElementById('btn-orders').classList.remove('hidden'); }
                    loadProducts();
                } catch (e) { showToast("Authentication Failed"); }
            }

            async function fetchNotifications() {
                try {
                    const res = await apiFetch('/api/notifications'); const notis = await res.json();
                    const badge = document.getElementById('noti-count');
                    const unread = notis.filter(n => !n.is_read).length;
                    if(unread > 0) { badge.innerText = unread; badge.classList.remove('hidden'); badge.classList.add('animate-bounce-short'); } else { badge.classList.add('hidden'); }
                    const list = document.getElementById('noti-list');
                    if(notis.length === 0) return list.innerHTML = `<div class="text-center text-slate-400 py-10 font-medium">အသိပေးချက် မရှိသေးပါ</div>`;
                    list.innerHTML = notis.map(n => `<div class="p-4 rounded-2xl border transition-all ${n.is_read ? 'bg-slate-50 border-slate-100 text-slate-500' : 'bg-indigo-50 border-indigo-100 text-indigo-900 shadow-sm'}"><div class="font-bold text-[13px] mb-1.5">${n.message}</div><div class="text-[10px] font-bold ${n.is_read ? 'text-slate-400' : 'text-indigo-500'}">${n.date}</div></div>`).join('');
                } catch(e) {}
            }
            function openNotiModal() { if(tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium'); document.getElementById('noti-modal').classList.add('active'); apiFetch('/api/notifications/read', { method: 'POST' }).then(() => document.getElementById('noti-count').classList.add('hidden')); }
            function closeNotiModal(e) { if(e.target === document.getElementById('noti-modal')) { document.getElementById('noti-modal').classList.remove('active'); fetchNotifications(); } }

            async function fetchLocationData() { 
                try { 
                    const res = await fetch('/api/locations'); mmData = await res.json(); 
                    document.getElementById('prof-store-state').innerHTML = getSelectOptions(mmData, "တိုင်း/ပြည်နယ် ရွေးရန်");
                } catch(e) {} 
            }
            function getSelectOptions(dataObj, defaultText) {
                let html = `<option value="">-- ${defaultText} --</option>`;
                if(dataObj) { if(Array.isArray(dataObj)) dataObj.forEach(v => html += `<option value="${v}">${v}</option>`); else for(let k in dataObj) html += `<option value="${k}">${k}</option>`; }
                return html;
            }
            function showTab(tabId, btnId) {
                if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
                document.querySelectorAll('.tab-content').forEach(el => { el.classList.add('hidden'); el.classList.remove('animate-fade-in'); });
                document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
                const tab = document.getElementById(tabId);
                tab.classList.remove('hidden'); void tab.offsetWidth; tab.classList.add('animate-fade-in');
                if(btnId) document.getElementById(btnId).classList.add('active');
                if(tabId === 'history-tab') loadBuyerOrders(); if(tabId === 'orders-tab') switchVendorTab('dash'); if(tabId === 'cart-tab') renderGroupedCart();
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }

            function triggerSell() { if(!currentUser.vendor_ready) document.getElementById('setup-modal').classList.add('active'); else tg.showConfirm("Bot Chat ထဲသို့ ပစ္စည်းပုံနှင့် ဈေးနှုန်းရေးပို့ပါ။ အခုပဲ App ကိုပိတ်ပြီး ပို့မလား?", (r) => { if(r) tg.close(); }); }
            
            function compressAndEncodeImage(el, targetId, maxD) {
                let file = el.files[0]; if(!file) return; 
                let reader = new FileReader();
                reader.onloadend = function(e) { 
                    let img = new Image();
                    img.onload = function() {
                        let canvas = document.createElement('canvas'); let ctx = canvas.getContext('2d');
                        let maxW = maxD; let maxH = maxD; let width = img.width; let height = img.height;
                        if (width > height) { if (width > maxW) { height *= maxW / width; width = maxW; } } else { if (height > maxH) { width *= maxH / height; height = maxH; } }
                        canvas.width = width; canvas.height = height; ctx.drawImage(img, 0, 0, width, height);
                        let dataUrl = canvas.toDataURL('image/jpeg', 0.8); 
                        document.getElementById(targetId).value = dataUrl; 
                        let preview = document.getElementById(targetId + '-preview');
                        if(preview) { preview.src = dataUrl; preview.classList.remove('hidden'); }
                    }; img.src = e.target.result;
                }; reader.readAsDataURL(file);
            }
            
            async function saveVendorProfile() {
                const sName = document.getElementById('prof-store-name').value.trim();
                const sState = document.getElementById('prof-store-state').value;
                const sDist = document.getElementById('prof-store-district').value;
                const sTsp = document.getElementById('prof-store-township').value;
                const sWard = document.getElementById('prof-store-ward').value.trim();
                const sStreet = document.getElementById('prof-store-street').value.trim();

                if(!sName || !sState || !sDist || !sTsp || !sWard || !sStreet) {
                    return showToast("⚠️ ဆိုင်အမည်၊ လမ်း/ရပ်ကွက် နှင့် တည်နေရာ အားလုံးကို ပြည့်စုံစွာ ဖြည့်ပါ။");
                }

                tg.MainButton.showProgress();
                const payload = { 
                    store_name: sName, store_state: sState, store_district: sDist, store_township: sTsp,
                    store_ward: sWard, store_street: sStreet, store_cover: document.getElementById('prof-store-cover').value,
                    accept_cod: document.getElementById('prof-cod').checked, 
                    kpay_phone: document.getElementById('prof-kpay-ph').value, wave_phone: document.getElementById('prof-wave-ph').value, 
                    kpay_qr: document.getElementById('prof-kpay-qr').value, wave_qr: document.getElementById('prof-wave-qr').value 
                };
                const res = await apiFetch('/api/vendor/profile', { method: 'POST', body: JSON.stringify(payload) }); 
                tg.MainButton.hideProgress();
                if(res.ok) { 
                    showToast("✅ ဆိုင်အချက်အလက် သိမ်းဆည်းပြီးပါပြီ။"); 
                    currentUser.vendor_ready = true; 
                    currentUser.store_name = sName;
                    currentUser.store_state = sState;
                    document.getElementById('btn-orders').classList.remove('hidden'); 
                }
            }

            // Advanced Location Filter Handlers
            function updateLocalTownships(stateVal) {
                localStateFilter = stateVal;
                localTownshipFilter = "All";
                document.getElementById('local-ward-filter').value = "";
                
                const advFilters = document.getElementById('advanced-location-filters');
                const tspSelect = document.getElementById('local-township-filter');
                
                if (stateVal === "All") {
                    advFilters.classList.add('hidden');
                    tspSelect.innerHTML = '<option value="All">မြို့နယ်အားလုံး</option>';
                } else {
                    advFilters.classList.remove('hidden');
                    let tHtml = '<option value="All">မြို့နယ်အားလုံး</option>';
                    const districts = mmData[stateVal];
                    for (let dist in districts) {
                        districts[dist].forEach(t => {
                            tHtml += `<option value="${t}">${t}</option>`;
                        });
                    }
                    tspSelect.innerHTML = tHtml;
                }
                triggerSearch();
            }

            function triggerSearch() { 
                clearTimeout(searchTimeout); 
                searchTimeout = setTimeout(() => { 
                    localTownshipFilter = document.getElementById('local-township-filter').value;
                    loadProducts(document.getElementById('search-box').value); 
                }, 400); 
            }

            async function loadProducts(query = "") {
                const wardFilter = document.getElementById('local-ward-filter') ? document.getElementById('local-ward-filter').value.trim() : "";
                const res = await apiFetch(`/api/products?category=${currentCategory}&search=${query}&state=${localStateFilter}&township=${localTownshipFilter}&ward=${encodeURIComponent(wardFilter)}`); 
                const data = await res.json(); allProducts = data.products;
                
                if(query === "" && localStateFilter === "All" && wardFilter === "") {
                    let catsHTML = `<button onclick="filterCategory('All')" class="btn-press cat-chip ${currentCategory==='All'?'active':''} px-5 py-2.5 rounded-full text-xs font-bold bg-white whitespace-nowrap shadow-sm text-slate-600">အားလုံး</button>`;
                    data.categories.forEach(c => catsHTML += `<button onclick="filterCategory('${c}')" class="btn-press cat-chip ${currentCategory===c?'active':''} px-5 py-2.5 rounded-full text-xs font-bold bg-white whitespace-nowrap shadow-sm text-slate-600">${c}</button>`);
                    document.getElementById('category-container').innerHTML = catsHTML;
                }

                let sf = document.getElementById('local-state-filter'); 
                if(sf.options.length <= 1 && data.states.length > 0) {
                    let currSfVal = sf.value;
                    let sfHtml = `<option value="All">နေရာအားလုံး (တိုင်း/ပြည်နယ်)</option>`;
                    data.states.forEach(s => { sfHtml += `<option value="${s}" ${currSfVal === s ? 'selected' : ''}>${s}</option>`; });
                    sf.innerHTML = sfHtml;
                }

                if(allProducts.length === 0) return document.getElementById('product-list').innerHTML = `<div class="col-span-2 text-center py-12 text-slate-400 font-medium animate-fade-up">ပစ္စည်းရှာမတွေ့ပါ 🔍</div>`;
                document.getElementById('product-list').innerHTML = allProducts.map((p, index) => generateProductCardHTML(p, index)).join('');
            }

            // EDGE-TO-EDGE + 5D SHADOW Implementation
            function generateProductCardHTML(p, index) {
                const imgSrc = p.img ? `/api/image/${p.img}` : 'https://via.placeholder.com/300'; 
                const isOut = p.stock <= 0; const animDelay = (index % 10) * 0.05; 
                const locBadge = p.vendor_state ? `<span class="absolute top-2 left-2 bg-indigo-600/90 backdrop-blur text-white px-2 py-1 rounded-md text-[9px] font-bold shadow-md z-30">📍 ${p.vendor_state.replace('တိုင်းဒေသကြီး', '').replace('ပြည်နယ်', '')}</span>` : '';
                
                return `<div class="animate-fade-up bg-white rounded-[24px] shadow-5d overflow-hidden flex flex-col relative transition-all duration-300 transform hover:-translate-y-2 ${isOut ? 'opacity-60 grayscale-[30%]' : ''}" style="animation-delay: ${animDelay}s">
                    ${isOut ? '<div class="absolute top-2 right-2 bg-red-500/90 backdrop-blur text-white text-[10px] font-black px-2 py-1 rounded-lg z-30 shadow-md">ကုန်နေပါသည်</div>' : ''}
                    
                    <div class="relative w-full pt-[100%] bg-slate-50 overflow-hidden shrink-0 group">
                        <img src="${imgSrc}" class="absolute inset-0 w-full h-full object-cover transition-transform duration-700 group-hover:scale-110 z-10">
                        <div class="absolute inset-0 bg-gradient-to-t from-black/40 via-transparent to-transparent z-20"></div>
                        ${locBadge}
                    </div>
                    
                    <div class="p-3.5 flex-grow flex flex-col justify-between bg-white z-20 relative">
                        <div class="mb-2">
                            <div class="text-[9px] font-extrabold text-slate-400 uppercase tracking-wider line-clamp-1 mb-1">🏪 ${p.vendor_name}</div>
                            <div class="text-[13px] font-extrabold text-slate-800 line-clamp-1 leading-snug mb-1.5">${p.name}</div>
                            <div class="flex justify-between items-end mb-2">
                                <div class="text-indigo-600 text-[15px] font-black leading-none">${p.price.toLocaleString()} <span class="text-[10px] font-bold">Ks</span></div>
                                <div class="text-[9px] font-bold ${isOut ? 'text-red-500 bg-red-50' : 'textemerald-600 bg-emerald-50'} px-1.5 py-0.5 rounded-md shrink-0 border ${isOut ? 'border-red-100' : 'border-emerald-100'}">📦 Stock: ${p.stock}</div>
                            </div>
                        </div>
                        <div class="flex gap-1.5 mt-1">
                            <button onclick='openStore(${p.vendor_id})' class="btn-press flex-1 bg-slate-50 border border-slate-200 text-slate-600 py-2.5 rounded-xl font-bold text-[10px] shadow-sm hover:bg-slate-100">🏪 ဆိုင်ပြခန်း</button>
                            <button onclick='addToCart(${JSON.stringify(p).replace(/'/g, "&#39;")})' class="btn-press flex-[1.5] ${isOut?'bg-slate-100 text-slate-400':'bg-indigo-600 text-white shadow-md shadow-indigo-500/30 hover:bg-indigo-700'} py-2.5 rounded-xl font-bold text-xs" ${isOut?'disabled':''}>🛒 ဝယ်မည်</button>
                        </div>
                    </div>
                </div>`
            }

            // EDGE-TO-EDGE + 5D SHADOW for Storefront Products
            function generateStorefrontProductCardHTML(p, index) {
                const imgSrc = p.img ? `/api/image/${p.img}` : 'https://via.placeholder.com/300'; 
                const isOut = p.stock <= 0; const animDelay = (index % 10) * 0.05; 
                
                return `<div class="animate-fade-up bg-white rounded-[24px] shadow-5d overflow-hidden flex flex-col relative transition-all duration-300 transform hover:-translate-y-2 ${isOut ? 'opacity-60 grayscale-[30%]' : ''}" style="animation-delay: ${animDelay}s">
                    ${isOut ? '<div class="absolute top-2 right-2 bg-red-500/90 backdrop-blur text-white text-[10px] font-black px-2 py-1 rounded-lg z-30 shadow-md">ကုန်နေပါသည်</div>' : ''}
                    
                    <div class="relative w-full pt-[100%] bg-slate-50 overflow-hidden shrink-0 group">
                        <img src="${imgSrc}" class="absolute inset-0 w-full h-full object-cover transition-transform duration-700 group-hover:scale-110 z-10">
                        <div class="absolute inset-0 bg-gradient-to-t from-black/40 via-transparent to-transparent z-20"></div>
                    </div>
                    
                    <div class="p-3.5 flex-grow flex flex-col justify-between bg-white z-20 relative">
                        <div class="mb-3">
                            <div class="text-[13px] font-extrabold text-slate-800 line-clamp-1 leading-snug mb-1.5">${p.name}</div>
                            <div class="flex justify-between items-end mb-1">
                                <div class="text-indigo-600 text-[15px] font-black leading-none">${p.price.toLocaleString()} <span class="text-[10px] font-bold">Ks</span></div>
                                <div class="text-[9px] font-bold ${isOut ? 'text-red-500 bg-red-50' : 'text-emerald-600 bg-emerald-50'} px-1.5 py-0.5 rounded-md shrink-0 border ${isOut ? 'border-red-100' : 'border-emerald-100'}">📦 Stock: ${p.stock}</div>
                            </div>
                        </div>
                        <div class="flex gap-1.5 mt-auto">
                            <button onclick='addToCart(${JSON.stringify(p).replace(/'/g, "&#39;")})' class="btn-press flex-[1] ${isOut?'bg-slate-100 text-slate-400':'bg-indigo-50 text-indigo-700 hover:bg-indigo-100'} py-2.5 rounded-xl font-extrabold text-[10px]" ${isOut?'disabled':''}>🛒 ခြင်းထဲထည့်မည်</button>
                            <button onclick='buyNow(${JSON.stringify(p).replace(/'/g, "&#39;")})' class="btn-press flex-[1.2] ${isOut?'bg-slate-100 text-slate-400':'bg-indigo-600 text-white shadow-md shadow-indigo-500/30 hover:bg-indigo-700'} py-2.5 rounded-xl font-extrabold text-[10px]" ${isOut?'disabled':''}>🛍️ ချက်ချင်းဝယ်မည်</button>
                        </div>
                    </div>
                </div>`
            }

            function filterCategory(cat) { currentCategory = cat; loadProducts(document.getElementById('search-box').value); }
            
            async function openStore(vendorId) {
                if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
                tg.MainButton.showProgress();
                try {
                    const res = await apiFetch(`/api/store/${vendorId}`); const data = await res.json();
                    document.getElementById('storefront-name').innerText = data.store.name;
                    const fullAddr = data.store.state ? `${data.store.street ? data.store.street+'၊ ':''}${data.store.ward ? data.store.ward+'၊ ':''}${data.store.township}၊ ${data.store.state}` : 'တည်နေရာ မသတ်မှတ်ရသေးပါ';
                    document.getElementById('storefront-location').innerText = fullAddr;
                    
                    const coverEl = document.getElementById('storefront-cover-img');
                    if(data.store.cover) coverEl.style.backgroundImage = `url('${data.store.cover}')`; else coverEl.style.backgroundImage = `url('https://via.placeholder.com/800x450')`;

                    storeViewProducts = data.products;
                    storeCurrentCat = "All";
                    
                    // Render Menu Categories
                    let catsHTML = `<button onclick="filterStoreMenu('All')" id="scat-All" class="btn-press px-5 py-2 rounded-xl text-xs font-bold whitespace-nowrap transition-all border border-transparent bg-indigo-600 text-white shadow-md">အားလုံး</button>`;
                    data.categories.forEach(c => catsHTML += `<button onclick="filterStoreMenu('${c}')" id="scat-${c.replace(/\s+/g, '-')}" class="btn-press px-5 py-2 rounded-xl text-xs font-bold whitespace-nowrap transition-all border border-slate-200 bg-white text-slate-600 shadow-sm">${c}</button>`);
                    document.getElementById('storefront-categories').innerHTML = catsHTML;
                    
                    renderStoreProducts();
                    const storeView = document.getElementById('storefront-view');
                    storeView.classList.remove('closing'); storeView.classList.add('active'); window.scrollTo({ top: 0 });
                } catch(e) { showToast("ဆိုင်အချက်အလက် ယူ၍မရပါ။"); } finally { tg.MainButton.hideProgress(); }
            }
            
            function filterStoreMenu(cat) {
                if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
                storeCurrentCat = cat;
                // Update UI Chips
                document.querySelectorAll('#storefront-categories button').forEach(b => {
                    b.className = "btn-press px-5 py-2 rounded-xl text-xs font-bold whitespace-nowrap transition-all border border-slate-200 bg-white text-slate-600 shadow-sm";
                });
                document.getElementById(`scat-${cat.replace(/\s+/g, '-')}`).className = "btn-press px-5 py-2 rounded-xl text-xs font-bold whitespace-nowrap transition-all border border-transparent bg-indigo-600 text-white shadow-md";
                document.getElementById('storefront-cat-title').innerText = cat === "All" ? "အားလုံး" : cat;
                renderStoreProducts();
            }

            function renderStoreProducts() {
                let filtered = storeCurrentCat === "All" ? storeViewProducts : storeViewProducts.filter(p => p.custom_category === storeCurrentCat);
                document.getElementById('storefront-count').innerText = `${filtered.length} Items`;
                document.getElementById('storefront-products').innerHTML = filtered.map((p, index) => generateStorefrontProductCardHTML(p, index)).join('');
            }

            function closeStore() { if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged(); const storeView = document.getElementById('storefront-view'); storeView.classList.add('closing'); setTimeout(() => { storeView.classList.remove('active'); storeView.classList.remove('closing'); }, 300); }
            
            function addToCart(prod) { 
                let ex = cart.find(i => i.id === prod.id); 
                if(ex) { if(ex.qty < prod.stock) ex.qty++; else return showToast("Stock မလုံလောက်ပါ။"); } else { cart.push({...prod, qty: 1}); }
                updateCartBadge(); showToast("🛒 ခြင်းထဲရောက်ပါပြီ"); 
            }

            function buyNow(prod) {
                addToCart(prod); 
                closeStore(); 
                showTab('cart-tab', 'btn-shop'); 
            }

            function updateCartBadge() { 
                const b = document.getElementById('cart-count'); let t = cart.reduce((s, i) => s + i.qty, 0); 
                b.innerText = t; 
                if(t > 0) { b.classList.remove('hidden'); b.classList.remove('animate-bounce-short'); void b.offsetWidth; b.classList.add('animate-bounce-short'); } else { b.classList.add('hidden'); }
            }

            function renderGroupedCart() {
                if(cart.length === 0) { document.getElementById('cart-empty-state').classList.remove('hidden'); document.getElementById('cart-content-wrapper').innerHTML = ''; return; }
                document.getElementById('cart-empty-state').classList.add('hidden');
                let vGroups = {};
                cart.forEach((i, idx) => {
                    if(!vGroups[i.vendor_id]) vGroups[i.vendor_id] = { vendor_name: i.vendor_name, vendor_state: i.vendor_state, vendor_cod: i.vendor_cod, kpay_phone: i.vendor_kpay, kpay_qr: i.kpay_qr, wave_phone: i.vendor_wave, wave_qr: i.wave_qr, items: [], total: 0 };
                    vGroups[i.vendor_id].items.push({...i, cart_idx: idx}); vGroups[i.vendor_id].total += (i.price * i.qty);
                });
                let html = '';
                for(let vid in vGroups) {
                    let g = vGroups[vid];
                    let itemsHtml = g.items.map(i => `
                    <div class="flex justify-between items-center mb-3 bg-white p-3 rounded-2xl shadow-[0_2px_8px_rgba(0,0,0,0.02)] border border-slate-100">
                        <div class="flex-1 pr-3"><div class="text-[13px] font-bold text-slate-800 line-clamp-1">${i.name}</div><div class="text-indigo-600 text-[12px] font-black mt-0.5">${i.price.toLocaleString()} Ks</div></div>
                        <div class="flex items-center gap-3 bg-slate-50 rounded-xl shadow-inner px-2 py-1 border border-slate-100">
                            <button onclick="changeQty(${i.cart_idx}, -1)" class="btn-press w-7 h-7 flex items-center justify-center text-slate-500 font-bold bg-white rounded-lg shadow-sm">-</button><span class="text-xs font-black w-3 text-center text-slate-700">${i.qty}</span><button onclick="changeQty(${i.cart_idx}, 1)" class="btn-press w-7 h-7 flex items-center justify-center text-indigo-600 font-bold bg-white rounded-lg shadow-sm">+</button>
                        </div>
                    </div>`).join('');
                    
                    let payHtml = `<div class="mt-4 border-t border-slate-100 pt-4">
                        <label class="text-xs font-bold text-slate-500 mb-2 block">ငွေချေစနစ်ရွေးချယ်ရန်</label>
                        <select id="pay_method_${vid}" onchange="togglePayMethod(${vid})" class="w-full p-3 bg-slate-50 border border-slate-200 rounded-xl text-[13px] font-bold mb-3 focus:ring-2 focus:ring-indigo-500 outline-none transition text-slate-700">`;
                    if(g.vendor_cod || (!g.kpay_phone && !g.wave_phone)) payHtml += `<option value="COD" selected>🏠 အိမ်ရောက်မှ ငွေချေမည် (COD)</option>`;
                    if(g.kpay_phone || g.kpay_qr) payHtml += `<option value="KPay">📲 KPay ဖြင့် ငွေလွှဲမည်</option>`;
                    if(g.wave_phone || g.wave_qr) payHtml += `<option value="Wave">📲 WavePay ဖြင့် ငွေလွှဲမည်</option>`;
                    payHtml += `</select>
                    <div id="qr_box_${vid}" class="bg-indigo-50/50 p-4 rounded-2xl mb-4 border border-indigo-100 hidden animate-fade-in">
                        <div class="flex justify-center mb-3"><img id="qr_img_${vid}" src="" class="max-h-36 rounded-xl shadow-md border border-white hidden"></div>
                        <p id="qr_phone_${vid}" class="text-center font-mono font-black text-xl text-indigo-900 tracking-wider bg-white py-2 rounded-xl border border-indigo-100 shadow-sm">-</p>
                        <div class="mt-4 bg-white p-3 rounded-xl border border-indigo-100 shadow-sm relative overflow-hidden"><div class="absolute top-0 left-0 w-1 h-full bg-indigo-500"></div><label class="block text-[11px] font-extrabold text-indigo-700 mb-2 pl-2">📸 ငွေလွှဲပြေစာ (Screenshot) တင်ရန် <span class="text-red-500">*လိုအပ်ပါသည်</span></label><input type="file" accept="image/*" id="slip_input_${vid}" onchange="compressAndEncodeImage(this, 'slip_base64_${vid}', 800)" class="text-xs text-slate-500 file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:text-[10px] file:font-bold file:bg-indigo-50 file:text-indigo-700 hover:file:bg-indigo-100 transition w-full outline-none"><input type="hidden" id="slip_base64_${vid}"><img id="slip_base64_${vid}-preview" class="h-24 object-cover rounded-xl hidden border border-slate-200 shadow-sm mt-3 ml-2"></div>
                        <input type="text" id="tx_id_${vid}" placeholder="ငွေလွှဲပြေစာ (Tx ID) ဂဏန်း ၆ လုံး..." class="w-full mt-3 p-3 border border-indigo-200 rounded-xl text-sm text-center outline-none focus:ring-2 focus:ring-indigo-500 font-bold text-indigo-900 placeholder-indigo-300">
                    </div></div>`;
                    
                    html += `<div class="animate-fade-up bg-white p-5 rounded-3xl shadow-sm border border-slate-100 mb-5">
                        <div class="flex justify-between items-center mb-4 pb-3 border-b border-slate-50"><div><h3 class="font-extrabold text-slate-800 flex items-center gap-1.5"><span class="text-lg">🏪</span> ${g.vendor_name}</h3><div class="text-[9px] font-bold text-slate-400 mt-1">📍 ${g.vendor_state || 'တည်နေရာ မသတ်မှတ်ရသေးပါ'}</div></div><span class="text-[15px] font-black gradient-text">${g.total.toLocaleString()} Ks</span></div>
                        ${itemsHtml}${payHtml}
                        <button onclick="checkoutVendor(${vid})" class="btn-press w-full gradient-bg text-white font-bold py-3.5 rounded-xl shadow-[0_4px_12px_rgba(99,102,241,0.3)] mt-2 text-sm tracking-wide">အော်ဒါတင်မည်</button>
                    </div>`;
                }
                
                html += `<div class="animate-fade-up bg-slate-50 border border-slate-200 p-5 rounded-3xl mb-5 mt-8 shadow-inner">
                    <h3 class="font-extrabold text-slate-700 mb-4 text-sm flex items-center gap-2"><span class="text-lg">📍</span> ပို့ဆောင်ရမည့် လိပ်စာအပြည့်အစုံ</h3>
                    <div class="space-y-3.5">
                        <select id="sel-state" onchange="updateAddr('sel-district', mmData[this.value])" class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-indigo-500 outline-none transition"></select>
                        <select id="sel-district" onchange="updateAddr('sel-township', mmData[document.getElementById('sel-state').value][this.value])" class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-indigo-500 outline-none transition"><option value="">-- ခရိုင် --</option></select>
                        <select id="sel-township" class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-indigo-500 outline-none transition"><option value="">-- မြို့နယ် --</option></select>
                        <input type="text" id="input-ward" placeholder="ရပ်ကွက် / ကျေးရွာ အမည်..." class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-indigo-500 outline-none transition">
                        <input type="text" id="input-street" placeholder="အိမ်အမှတ်၊ လမ်းအမည်၊ အထင်ကရနေရာ..." class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-indigo-500 outline-none transition">
                        <input type="tel" id="input-phone" placeholder="ဆက်သွယ်ရမည့် ဖုန်းနံပါတ်..." value="${currentUser.phone||''}" class="w-full p-3 bg-white rounded-xl border border-slate-200 text-sm font-medium focus:ring-2 focus:ring-indigo-500 outline-none transition">
                    </div>
                </div>`;
                document.getElementById('cart-content-wrapper').innerHTML = html; 
                document.getElementById('sel-state').innerHTML = getSelectOptions(mmData, "တိုင်းဒေသကြီး/ပြည်နယ်");
                for(let vid in vGroups) togglePayMethod(vid, vGroups[vid]);
            }

            function updateAddr(tId, sData) { document.getElementById(tId).innerHTML = getSelectOptions(sData, "ရွေးချယ်ပါ"); }
            function changeQty(idx, d) { if(d > 0 && cart[idx].qty >= cart[idx].stock) return showToast("Stock မလုံလောက်ပါ။"); cart[idx].qty += d; if(cart[idx].qty <= 0) cart.splice(idx, 1); updateCartBadge(); renderGroupedCart(); }
            function togglePayMethod(vid, gData) {
                const selectElement = document.getElementById(`pay_method_${vid}`); if(!selectElement) return;
                const m = selectElement.value; const box = document.getElementById(`qr_box_${vid}`);
                if(m === "COD") { box.classList.add("hidden"); } else {
                    box.classList.remove("hidden"); 
                    if(!gData) { let f = cart.find(i => i.vendor_id == vid); gData = {kpay_phone: f.vendor_kpay, wave_phone: f.vendor_wave, kpay_qr: f.kpay_qr, wave_qr: f.wave_qr}; }
                    const img = document.getElementById(`qr_img_${vid}`), ph = document.getElementById(`qr_phone_${vid}`);
                    if(m === "KPay") { img.src = gData.kpay_qr || ""; img.classList.toggle("hidden", !gData.kpay_qr); ph.innerText = gData.kpay_phone || "-"; } 
                    else if(m === "Wave") { img.src = gData.wave_qr || ""; img.classList.toggle("hidden", !gData.wave_qr); ph.innerText = gData.wave_phone || "-"; }
                }
            }

            async function checkoutVendor(vid) {
                const st = document.getElementById('sel-state').value, 
                      dist = document.getElementById('sel-district').value, 
                      tsp = document.getElementById('sel-township').value, 
                      ward = document.getElementById('input-ward').value.trim(),
                      str = document.getElementById('input-street').value.trim(), 
                      ph = document.getElementById('input-phone').value.trim();
                
                if(!st || !dist || !tsp || !ward || !str || !ph) return showToast("ရပ်ကွက်နှင့် လမ်းနာမည် အပါအဝင် လိပ်စာအပြည့်အစုံကို သေချာဖြည့်ပါ။");
                
                const m = document.getElementById(`pay_method_${vid}`).value; 
                let txId = "", slipBase64 = "";
                
                if(m !== "COD") { 
                    txId = document.getElementById(`tx_id_${vid}`).value.trim(); 
                    slipBase64 = document.getElementById(`slip_base64_${vid}`).value;
                    if(!slipBase64) return showToast("⚠️ လုံခြုံရေးအရ ငွေလွှဲပြေစာ (Screenshot) ပုံတင်ပေးရန် မဖြစ်မနေ လိုအပ်ပါသည်။"); 
                }
                
                tg.MainButton.showProgress();
                try {
                    const fullAddress = `${str}၊ ${ward}၊ ${tsp}၊ ${dist}၊ ${st}။`;
                    const payloadData = { 
                        transaction_id: txId, payment_slip: slipBase64, 
                        address: fullAddress, phone: ph, payment_method: m, 
                        cart: cart.filter(i => i.vendor_id == vid).map(i=>({id:i.id, qty:i.qty})) 
                    };
                    
                    const res = await apiFetch(`/api/checkout`, { method: 'POST', body: JSON.stringify(payloadData) });
                    if(res.ok) { 
                        cart = cart.filter(i => i.vendor_id != vid); updateCartBadge(); showToast("✅ အော်ဒါတင်ခြင်း အောင်မြင်ပါသည်။"); 
                        if(cart.length === 0) showTab('history-tab', 'btn-history'); else renderGroupedCart(); 
                    } else { 
                        let errMsg = "အမှားအယွင်းဖြစ်ပေါ်ခဲ့ပါသည်။";
                        try { const errData = await res.json(); if(errData.detail) errMsg = errData.detail; } catch(err) {}
                        showToast("⚠️ " + errMsg);
                    }
                } catch(e) { showToast("⚠️ အင်တာနက်ချိတ်ဆက်မှု ပြတ်တောက်သွားပါသည်။"); } 
                finally { tg.MainButton.hideProgress(); }
            }

            async function loadBuyerOrders() {
                const res = await apiFetch('/api/buyer/orders'); const orders = await res.json();
                const trackIndex = { 'pending': 1, 'approved': 2, 'shipped': 3, 'delivered': 4, 'cancelled': 0 };
                document.getElementById('buyer-order-list').innerHTML = orders.map((o, index) => {
                    let level = trackIndex[o.status]; let progressWidth = level === 0 ? 0 : ((level - 1) / 3) * 100;
                    let trackerHtml = o.status === 'cancelled' ? `<div class="text-center my-4"><span class="status-cancelled">❌ ဤအော်ဒါအား ပယ်ဖျက်လိုက်ပါသည်</span></div>` : `<div class="tracker-container"><div class="tracker-line"></div><div class="tracker-progress" style="width: ${progressWidth}%;"></div><div class="track-step ${level >= 1 ? 'active' : ''}"><div class="track-dot">✓</div><span class="track-label">စစ်ဆေးဆဲ</span></div><div class="track-step ${level >= 2 ? 'active' : ''}"><div class="track-dot">📦</div><span class="track-label">ထုပ်ပိုးဆဲ</span></div><div class="track-step ${level >= 3 ? 'active' : ''}"><div class="track-dot">🚚</div><span class="track-label">ပို့နေပါပြီ</span></div><div class="track-step ${level >= 4 ? 'active' : ''}"><div class="track-dot">🎁</div><span class="track-label">ရောက်ပါပြီ</span></div></div>`;
                    const animDelay = (index % 10) * 0.1;
                    return `<div class="animate-fade-up bg-white p-5 rounded-3xl shadow-[0_2px_12px_rgba(0,0,0,0.03)] border border-slate-100" style="animation-delay: ${animDelay}s">
                        <div class="flex justify-between items-start mb-3 border-b border-slate-50 pb-3"><span class="text-[14px] font-extrabold text-slate-800 leading-snug">${o.name} <span class="text-indigo-600 bg-indigo-50 px-1.5 py-0.5 rounded-md text-[11px] ml-1">x${o.qty}</span></span><span class="font-black text-slate-800 ml-3 shrink-0">${(o.price * o.qty).toLocaleString()} Ks</span></div>
                        ${trackerHtml}
                        <div class="flex justify-between items-center text-[10px] font-bold text-slate-400 mt-4 bg-slate-50 p-2 rounded-xl"><span class="flex items-center gap-1">${o.pay === 'COD' ? '🏠 COD စနစ်' : '💳 QR ဖြင့်ချေထားသည်'}</span><span>📅 ${o.date}</span></div>
                    </div>`;
                }).join('');
            }
            
            function switchVendorTab(tab) {
                ['dash', 'prods', 'profile'].forEach(t => { 
                    const btn = document.getElementById(`v-tab-${t}`);
                    btn.className = tab === t ? 'btn-press flex-1 bg-white shadow-[0_2px_8px_rgba(0,0,0,0.05)] py-2.5 rounded-xl text-sm font-extrabold text-indigo-600 transition-all' : 'btn-press flex-1 py-2.5 rounded-xl text-sm font-bold text-slate-500 transition-all hover:bg-slate-200/50'; 
                    document.getElementById(`vendor-${t}-view`).style.display = tab === t ? 'block' : 'none'; 
                });
                if(tab === 'dash') loadVendorOrders(); else if(tab === 'prods') loadVendorProducts();
            }
            
            async function loadVendorOrders() {
                const res = await apiFetch('/api/vendor/orders'); const orders = await res.json();
                document.getElementById('order-list').innerHTML = orders.map((o, index) => {
                    const animDelay = (index % 10) * 0.1;
                    let slipBtnHtml = ''; if(o.slip_img) slipBtnHtml = `<button onclick="viewSlip(this.getAttribute('data-img'))" data-img="${o.slip_img}" class="bg-emerald-100 text-emerald-700 hover:bg-emerald-200 px-2 py-1 rounded-lg ml-1 text-[10px] font-extrabold border border-emerald-200 transition btn-press shadow-sm">📸 ပြေစာကြည့်မည်</button>`;
                    return `<div class="animate-fade-up bg-white p-4 rounded-3xl shadow-sm border border-slate-100 mb-4" style="animation-delay: ${animDelay}s">
                    <div class="flex justify-between items-start mb-3"><div class="text-sm font-extrabold text-slate-800 pr-2">${o.name} <span class="text-indigo-600">x${o.qty}</span></div><div class="text-[10px] uppercase font-black px-2.5 py-1 rounded-lg ${o.status==='pending'?'bg-amber-100 text-amber-700':o.status==='cancelled'?'bg-red-100 text-red-700':'bg-emerald-100 text-emerald-700'}">${o.status}</div></div>
                    <div class="bg-slate-50 p-3 rounded-2xl text-[12px] font-medium text-slate-600 mb-3 border border-slate-100 space-y-1.5 leading-relaxed">
                        <div class="flex items-center gap-1.5"><span class="text-slate-400">👤</span> ${o.buyer}</div>
                        <div class="flex items-center gap-1.5 flex-wrap"><span class="text-slate-400">💳</span> ${o.pay === 'COD' ? '🏠 COD' : 'TxID: <b class="text-slate-800 font-mono tracking-wide">' + (o.tx || '-') + '</b>'} ${slipBtnHtml}</div>
                        <div class="flex items-start gap-1.5"><span class="text-slate-400 mt-0.5">📍</span> <span class="line-clamp-2">${o.addr}</span></div>
                    </div>
                    <select onchange="updateOrderStatus(${o.id}, this.value, this)" class="w-full bg-white border border-indigo-200 text-indigo-700 p-3 rounded-xl text-xs font-bold outline-none focus:ring-2 focus:ring-indigo-500 shadow-[0_2px_4px_rgba(99,102,241,0.05)] transition">
                        <option value="pending" ${o.status==='pending'?'selected':''}>⏳ စစ်ဆေးဆဲ</option>
                        <option value="approved" ${o.status==='approved'?'selected':''}>📦 အတည်ပြုမည် (ထုပ်ပိုးမည်)</option>
                        <option value="shipped" ${o.status==='shipped'?'selected':''}>🚚 ပို့ဆောင်လိုက်ပြီ</option>
                        <option value="delivered" ${o.status==='delivered'?'selected':''}>✅ ရောက်ရှိပါပြီ</option>
                        <option value="cancelled" ${o.status==='cancelled'?'selected':''}>❌ ပယ်ဖျက်မည်</option>
                    </select>
                </div>`}).join('');
            }
            
            function viewSlip(imgData) { document.getElementById('slip-viewer-img').src = imgData; document.getElementById('slip-viewer-modal').classList.add('active'); }
            function closeSlipModal() { document.getElementById('slip-viewer-modal').classList.remove('active'); setTimeout(() => { document.getElementById('slip-viewer-img').src = ''; }, 300); }

            function updateOrderStatus(id, st, selectElement) {
                const statusNames = { 'pending': '⏳ စစ်ဆေးဆဲ', 'approved': '📦 အတည်ပြုမည် (ထုပ်ပိုးမည်)', 'shipped': '🚚 ပို့ဆောင်လိုက်ပြီ', 'delivered': '✅ ရောက်ရှိပါပြီ', 'cancelled': '❌ ပယ်ဖျက်မည်' };
                tg.showConfirm(`ဤအော်ဒါကို '${statusNames[st]}' အဖြစ် အတည်ပြုပြောင်းလဲမှာ သေချာပါသလား?`, async function(confirm) {
                    if(confirm) {
                        tg.MainButton.showProgress();
                        try { const res = await apiFetch(`/api/vendor/orders/${id}/status?status=${st}`, {method:'POST'}); if(res.ok) { showToast("✅ အခြေအနေ ပြောင်းလဲပြီးပါပြီ"); } else { showToast("⚠️ လုပ်ပိုင်ခွင့် မရှိပါ သို့မဟုတ် အမှားအယွင်းဖြစ်နေပါသည်။"); } } catch(e) { showToast("⚠️ အင်တာနက်ချိတ်ဆက်မှု ပြတ်တောက်သွားပါသည်။"); }
                        tg.MainButton.hideProgress(); loadVendorOrders();
                    } else { loadVendorOrders(); }
                });
            }
            
            let vendorProducts = [];
            async function loadVendorProducts() {
                const res = await apiFetch('/api/vendor/products'); const data = await res.json();
                vendorProducts = data.products;
                
                // Populate datalist for category selection
                let catOpts = "";
                data.categories.forEach(c => { if(c) catOpts += `<option value="${c}">`; });
                document.getElementById('vendor-categories-list').innerHTML = catOpts;

                document.getElementById('vendor-product-list').innerHTML = vendorProducts.map((p, index) => {
                    const imgSrc = p.img ? `/api/image/${p.img}` : 'https://via.placeholder.com/300'; const animDelay = (index % 10) * 0.1;
                    return `<div class="animate-fade-up bg-white p-3.5 rounded-3xl shadow-sm border border-slate-100 mb-3 relative overflow-hidden" style="animation-delay: ${animDelay}s">
                        <div class="flex gap-4 items-center">
                            <img src="${imgSrc}" class="w-20 h-20 object-cover rounded-2xl shadow-sm border border-slate-100 bg-slate-50">
                            <div class="flex-1">
                                <div class="text-[13px] font-extrabold text-slate-800 line-clamp-1 mb-1">${p.name}</div>
                                <div class="text-indigo-600 text-sm font-black mb-1.5">${p.price.toLocaleString()} Ks</div>
                                <div class="flex items-center gap-1.5">
                                    <span class="text-[9px] text-slate-500 font-extrabold bg-slate-100 px-2 py-0.5 rounded-md border border-slate-200">📦 Stock: ${p.stock}</span>
                                    <span class="text-[9px] text-indigo-500 font-extrabold bg-indigo-50 px-2 py-0.5 rounded-md border border-indigo-100 flex items-center"><span class="mr-0.5">🏷️</span> ${p.custom_category || 'General'}</span>
                                </div>
                            </div>
                        </div>
                        <div class="flex gap-2 mt-3 border-t border-slate-50 pt-3">
                            <button onclick="quickMoveCategory(${p.id})" class="btn-press flex-[1.5] bg-indigo-600 text-white px-3 py-1.5 rounded-xl text-[11px] font-bold shadow-md shadow-indigo-500/30">Menu ပြောင်းမည်</button>
                            <button onclick='openEditModal(${JSON.stringify(p).replace(/'/g, "&#39;")})' class="btn-press flex-1 bg-indigo-50 text-indigo-600 px-3 py-1.5 rounded-xl text-[11px] font-extrabold hover:bg-indigo-100">ပြင်မည်</button>
                            <button onclick="if(confirm('ဤပစ္စည်းကို ဖျက်မှာ သေချာပါသလား?')) apiFetch('/api/vendor/products/${p.id}', {method:'DELETE'}).then(loadVendorProducts)" class="btn-press flex-1 text-red-500 bg-red-50 px-3 py-1.5 rounded-xl text-[11px] font-extrabold hover:bg-red-100">ဖျက်မည်</button>
                        </div>
                    </div>`
                }).join('');
            }

            async function quickMoveCategory(prodId) {
                const targetCat = document.getElementById('quick-cat-input').value.trim();
                if(!targetCat) return showToast("⚠️ အထက်ပါအကွက်တွင် ပြောင်းလိုသော အမျိုးအစား (Menu) ကို ရိုက်ထည့်/ရွေးချယ်ပါ။");
                
                tg.MainButton.showProgress();
                const res = await apiFetch(`/api/vendor/products/${prodId}`, { method: 'PUT', body: JSON.stringify({ custom_category: targetCat }) });
                tg.MainButton.hideProgress();
                
                if(res.ok) { 
                    showToast(`✅ အမျိုးအစားကို "${targetCat}" သို့ပြောင်းရွှေ့ပြီးပါပြီ`); 
                    loadVendorProducts(); 
                } else { showToast("⚠️ အမှားအယွင်းဖြစ်ပေါ်ခဲ့ပါသည်။"); }
            }

            function openEditModal(prod) {
                document.getElementById('edit-prod-id').value = prod.id; 
                document.getElementById('edit-prod-name').value = prod.name; 
                document.getElementById('edit-prod-price').value = prod.price; 
                document.getElementById('edit-prod-stock').value = prod.stock; 
                document.getElementById('edit-prod-cat').value = prod.custom_category || 'General';
                document.getElementById('edit-product-modal').classList.add('active');
            }
            function closeEditModal() { document.getElementById('edit-product-modal').classList.remove('active'); }

            async function saveEditProduct() {
                const id = document.getElementById('edit-prod-id').value, 
                      name = document.getElementById('edit-prod-name').value.trim(), 
                      price = document.getElementById('edit-prod-price').value, 
                      stock = document.getElementById('edit-prod-stock').value,
                      cat = document.getElementById('edit-prod-cat').value.trim() || 'General';
                      
                if(!name || !price || !stock) return showToast("အချက်အလက် ပြည့်စုံစွာ ဖြည့်ပါ။");
                tg.MainButton.showProgress(); 
                const res = await apiFetch(`/api/vendor/products/${id}`, { method: 'PUT', body: JSON.stringify({ name: name, price: price, stock: stock, custom_category: cat }) }); 
                tg.MainButton.hideProgress();
                if(res.ok) { showToast("✅ ပစ္စည်းအချက်အလက် ပြင်ဆင်ပြီးပါပြီ"); closeEditModal(); loadVendorProducts(); loadProducts(); } else { showToast("⚠️ အမှားအယွင်းဖြစ်ပေါ်ခဲ့ပါသည်။"); }
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
