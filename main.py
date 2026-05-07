import os
import hmac
import hashlib
import json
import threading
import datetime
import time
import secrets
import requests
from urllib.parse import parse_qs
from fastapi import FastAPI, Depends, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime, Boolean, Text, text
from sqlalchemy.orm import sessionmaker, Session, relationship, declarative_base
from telebot import TeleBot, types

# ==========================================
# 1. CORE CONFIGURATION
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://your-render-app-url.onrender.com")
ADMIN_TELEGRAM_ID = os.environ.get("ADMIN_TELEGRAM_ID", "YOUR_ID") 

bot = TeleBot(BOT_TOKEN)
app = FastAPI(title="Premium Smart Dine-In System")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_headers=["*"], allow_methods=["*"])

# ==========================================
# 2. DATABASE MODELS (Production-Ready)
# ==========================================
DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR}/premium_restaurant_pro.db")

# FIX FOR RENDER.COM POSTGRESQL (check_same_thread issue)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False, "timeout": 15} if "sqlite" in DATABASE_URL else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base() 

class Staff(Base):
    __tablename__ = "staff"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, index=True)
    full_name = Column(String)
    role = Column(String, default="admin") # admin, waiter, kitchen

class MenuItem(Base):
    __tablename__ = "menu_items"
    id = Column(Integer, primary_key=True)
    name = Column(String, index=True)
    description = Column(String, default="")
    price_thb = Column(Float, nullable=False)
    category = Column(String, default="Main Course", index=True)
    image_base64 = Column(Text, default="")
    dietary_tags = Column(String, default="") # e.g., "Vegan, Gluten-Free"
    is_available = Column(Boolean, default=True)

class RestaurantTable(Base):
    __tablename__ = "restaurant_tables"
    id = Column(Integer, primary_key=True)
    table_number = Column(String, unique=True, index=True)
    secure_token = Column(String, unique=True, index=True) # Cryptographic URL Token
    status = Column(String, default="available") # available, occupied, needs_service, requested_bill

class DineInOrder(Base):
    __tablename__ = "dine_in_orders"
    id = Column(Integer, primary_key=True)
    table_id = Column(Integer, ForeignKey("restaurant_tables.id"))
    total_thb = Column(Float, default=0.0)
    status = Column(String, default="received") # received, cooking, served, paid
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    table = relationship("RestaurantTable")
    items = relationship("OrderItem", back_populates="order")

class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("dine_in_orders.id"))
    menu_item_id = Column(Integer, ForeignKey("menu_items.id"))
    quantity = Column(Integer, default=1)
    price_at_order = Column(Float)
    special_requests = Column(String, default="")
    order = relationship("DineInOrder", back_populates="items")
    menu_item = relationship("MenuItem")

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# ==========================================
# 3. TELEGRAM AUTHENTICATION (STAFF ONLY)
# ==========================================
def get_current_staff(x_telegram_init_data: str = Header(None), db: Session = Depends(get_db)):
    if not x_telegram_init_data: raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        vals = {k: v[0] for k, v in parse_qs(x_telegram_init_data).items()}
        hash_str = vals.pop('hash', None)
        data_check_str = "\n".join([f"{k}={v}" for k, v in sorted(vals.items())])
        secret_key = hmac.new("WebAppData".encode(), BOT_TOKEN.encode(), hashlib.sha256).digest()
        hmac_res = hmac.new(secret_key, data_check_str.encode(), hashlib.sha256).hexdigest()
        if hmac_res != hash_str: raise HTTPException(status_code=401)
        tg_user = json.loads(vals['user'])
    except: raise HTTPException(status_code=401)
    
    db_staff = db.query(Staff).filter(Staff.telegram_id == str(tg_user['id'])).first()
    if not db_staff:
        db_staff = Staff(telegram_id=str(tg_user['id']), full_name=tg_user.get('first_name', 'Staff'))
        db.add(db_staff)
        db.commit()
        db.refresh(db_staff)
    return db_staff

# ==========================================
# 4. GUEST / DINE-IN APIs (SECURED BY TOKEN)
# ==========================================
@app.get("/api/guest/table/{secure_token}")
def verify_table(secure_token: str, db: Session = Depends(get_db)):
    table = db.query(RestaurantTable).filter(RestaurantTable.secure_token == secure_token).first()
    if not table: raise HTTPException(status_code=404, detail="Invalid QR Code")
    return {"table_id": table.id, "table_number": table.table_number, "status": table.status}

@app.get("/api/guest/menu")
def get_menu_guest(db: Session = Depends(get_db)):
    items = db.query(MenuItem).filter(MenuItem.is_available == True).all()
    categories = list(set([i.category for i in items]))
    return {"categories": categories, "items": [{"id": i.id, "name": i.name, "description": i.description, "price": i.price_thb, "category": i.category, "tags": i.dietary_tags, "image": i.image_base64} for i in items]}

@app.post("/api/guest/order")
async def place_order(req: Request, db: Session = Depends(get_db)):
    data = await req.json()
    secure_token = data.get('secure_token')
    cart = data.get('cart', [])
    
    table = db.query(RestaurantTable).filter(RestaurantTable.secure_token == secure_token).first()
    if not table or not cart: raise HTTPException(status_code=400, detail="Invalid Order")

    new_order = DineInOrder(table_id=table.id, total_thb=0.0)
    db.add(new_order)
    db.flush()

    total_thb = 0.0
    receipt_lines = []
    
    for item in cart:
        menu_item = db.query(MenuItem).filter(MenuItem.id == item['id']).first()
        if menu_item and menu_item.is_available:
            qty = item.get('qty', 1)
            notes = item.get('notes', '').strip()
            total_thb += (menu_item.price_thb * qty)
            db.add(OrderItem(order_id=new_order.id, menu_item_id=menu_item.id, quantity=qty, price_at_order=menu_item.price_thb, special_requests=notes))
            
            note_str = f"\n   *Note: {notes}*" if notes else ""
            receipt_lines.append(f"▫️ {qty}x {menu_item.name}{note_str}")

    new_order.total_thb = total_thb
    table.status = "occupied"
    db.commit()

    # DISPATCH TO TELEGRAM (STAFF)
    staff_members = db.query(Staff).all()
    dispatch_msg = f"🔔 **NEW ORDER | Table {table.table_number}**\n\n" + "\n".join(receipt_lines) + f"\n\n💰 **Total: ฿{total_thb:,.2f}**"
    
    for staff in staff_members:
        try: bot.send_message(staff.telegram_id, dispatch_msg, parse_mode="Markdown")
        except: pass 
        
    return {"status": "success", "order_id": new_order.id}

@app.post("/api/guest/service")
async def request_service(req: Request, db: Session = Depends(get_db)):
    data = await req.json()
    secure_token = data.get('secure_token')
    service_type = data.get('type') # 'waiter' or 'bill'
    
    table = db.query(RestaurantTable).filter(RestaurantTable.secure_token == secure_token).first()
    if not table: raise HTTPException(status_code=400)
    
    table.status = "needs_service" if service_type == "waiter" else "requested_bill"
    db.commit()

    alert_icon = "🙋‍♂️" if service_type == "waiter" else "💳"
    alert_text = "Waitstaff Requested" if service_type == "waiter" else "Bill Requested"
    
    for staff in db.query(Staff).all():
        try: bot.send_message(staff.telegram_id, f"{alert_icon} **{alert_text} | Table {table.table_number}**\n_Please attend to the guests immediately._", parse_mode="Markdown")
        except: pass
        
    return {"status": "success"}

# ==========================================
# 5. STAFF DASHBOARD APIs
# ==========================================
@app.get("/api/staff/tables")
def get_staff_tables(staff: Staff = Depends(get_current_staff), db: Session = Depends(get_db)):
    tables = db.query(RestaurantTable).all()
    return [{"id": t.id, "number": t.table_number, "status": t.status, "token": t.secure_token} for t in tables]

@app.post("/api/staff/tables")
async def add_table(req: Request, staff: Staff = Depends(get_current_staff), db: Session = Depends(get_db)):
    data = await req.json()
    new_token = secrets.token_urlsafe(16)
    db.add(RestaurantTable(table_number=data['table_number'], secure_token=new_token))
    db.commit()
    return {"status": "success"}

@app.post("/api/staff/tables/{table_id}/clear")
def clear_table(table_id: int, staff: Staff = Depends(get_current_staff), db: Session = Depends(get_db)):
    table = db.query(RestaurantTable).filter(RestaurantTable.id == table_id).first()
    if table: table.status = "available"; db.commit()
    return {"status": "success"}

@app.post("/api/staff/menu")
async def add_menu_item(req: Request, staff: Staff = Depends(get_current_staff), db: Session = Depends(get_db)):
    data = await req.json()
    db.add(MenuItem(name=data['name'], description=data.get('description',''), price_thb=float(data['price']), category=data.get('category','Main'), dietary_tags=data.get('tags',''), image_base64=data.get('image','')))
    db.commit()
    return {"status": "success"}

# ==========================================
# 6. FRONTEND (PREMIUM GUEST UI & STAFF UI)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,600;0,700;1,400&family=Inter:wght@300;400;600;800&display=swap" rel="stylesheet">
        <title>Coral Beach - Premium Dine-In</title>
        <style>
            :root { --gold: #D4AF37; --dark: #0f1115; --slate: #1e2430; }
            body { font-family: 'Inter', sans-serif; background-color: var(--dark); color: #f8fafc; -webkit-tap-highlight-color: transparent; }
            h1, h2, h3, .serif { font-family: 'Playfair Display', serif; }
            
            .gold-gradient { background: linear-gradient(135deg, #F3E5AB, #D4AF37, #C5A028); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .bg-gold { background: linear-gradient(135deg, #D4AF37, #C5A028); }
            
            .glass-card { background: rgba(30, 36, 48, 0.6); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px); border: 1px solid rgba(255, 255, 255, 0.05); border-radius: 20px; }
            .btn-press:active { transform: scale(0.96); transition: transform 0.1s ease; }
            
            .modal { position: fixed; inset: 0; background: rgba(0,0,0,0.85); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); z-index: 100; display: none; flex-direction: column; justify-content: flex-end; }
            .modal.active { display: flex; animation: fadeIn 0.3s; }
            .modal-content { background: var(--slate); border-top-left-radius: 28px; border-top-right-radius: 28px; padding: 28px; border-top: 1px solid rgba(212, 175, 55, 0.2); box-shadow: 0 -10px 40px rgba(0,0,0,0.5); }
            
            @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
            @keyframes slideUp { from { transform: translateY(20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
            .animate-slide-up { animation: slideUp 0.5s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
            
            #toast { visibility: hidden; background: #D4AF37; color: #000; text-align: center; border-radius: 12px; padding: 12px 20px; position: fixed; z-index: 1000; left: 50%; top: 40px; transform: translateX(-50%); font-weight: 800; font-size: 14px; box-shadow: 0 10px 25px rgba(212, 175, 55, 0.3); }
            #toast.show { visibility: visible; animation: fadeIn 0.3s, fadeIn 0.3s 3s reverse forwards; }
            
            /* Custom Scrollbar for sleek UI */
            ::-webkit-scrollbar { width: 4px; height: 4px; }
            ::-webkit-scrollbar-thumb { background: rgba(212, 175, 55, 0.3); border-radius: 10px; }
        </style>
    </head>
    <body class="pb-28">
        <div id="toast">Message</div>

        <div id="guest-view" class="hidden">
            <header class="pt-8 pb-4 px-6 flex justify-between items-center bg-gradient-to-b from-black/80 to-transparent">
                <div>
                    <h1 class="text-3xl font-bold gold-gradient mb-1">Coral Beach</h1>
                    <p class="text-[10px] tracking-[0.2em] text-slate-400 uppercase font-semibold">Restaurant & Lounge</p>
                </div>
                <div class="px-4 py-2 rounded-xl border border-[#D4AF37]/30 bg-[#D4AF37]/10 flex flex-col items-center justify-center shadow-[0_0_15px_rgba(212,175,55,0.1)]">
                    <span class="text-[9px] text-[#D4AF37] uppercase tracking-widest mb-0.5">Table</span>
                    <span id="display-table-num" class="text-white font-bold text-lg leading-none">--</span>
                </div>
            </header>

            <div class="sticky top-0 z-40 bg-dark/95 backdrop-blur-md border-b border-white/5 py-4 px-6 flex gap-3 overflow-x-auto scrollbar-hide" id="category-nav">
                </div>

            <div id="guest-menu" class="p-5 space-y-5">
                </div>

            <div class="fixed bottom-0 w-full glass-card rounded-none border-t border-[#D4AF37]/10 p-5 flex justify-between items-center z-50 pb-safe shadow-[0_-10px_30px_rgba(0,0,0,0.5)]">
                <div class="flex gap-3">
                    <button onclick="requestService('waiter')" class="w-14 h-14 rounded-2xl border border-white/10 flex flex-col items-center justify-center bg-slate-800/80 btn-press hover:bg-slate-700 transition">
                        <span class="text-xl mb-0.5">🙋‍♂️</span>
                        <span class="text-[9px] font-bold text-slate-300 uppercase tracking-wide">Call</span>
                    </button>
                    <button onclick="requestService('bill')" class="w-14 h-14 rounded-2xl border border-white/10 flex flex-col items-center justify-center bg-slate-800/80 btn-press hover:bg-slate-700 transition">
                        <span class="text-xl mb-0.5">💳</span>
                        <span class="text-[9px] font-bold text-slate-300 uppercase tracking-wide">Bill</span>
                    </button>
                </div>
                <button onclick="openCart()" class="bg-gold text-dark font-bold px-7 py-4 rounded-2xl flex items-center gap-4 btn-press shadow-[0_4px_25px_rgba(212,175,55,0.4)]">
                    <span class="text-base tracking-wide">View Order</span>
                    <div id="cart-badge" class="bg-dark text-white text-[11px] font-black w-6 h-6 rounded-full flex items-center justify-center hidden shadow-inner">0</div>
                </button>
            </div>
        </div>

        <div id="cart-modal" class="modal" onclick="closeCart(event)">
            <div class="modal-content" onclick="event.stopPropagation()">
                <div class="flex justify-between items-center mb-8">
                    <h2 class="text-2xl font-bold gold-gradient tracking-wide">Your Order</h2>
                    <button onclick="closeCart()" class="w-8 h-8 rounded-full bg-white/10 text-white flex items-center justify-center text-xl btn-press">&times;</button>
                </div>
                <div id="cart-items" class="max-h-[50vh] overflow-y-auto space-y-4 mb-8 pr-2"></div>
                <div class="flex justify-between items-end border-t border-[#D4AF37]/20 pt-6 mb-8">
                    <span class="text-slate-400 font-bold tracking-wider uppercase text-sm">Total Amount</span>
                    <span id="cart-total" class="text-3xl font-black text-white tracking-tight">฿0.00</span>
                </div>
                <button onclick="submitOrder()" class="w-full bg-gold text-dark font-black tracking-wide py-4.5 rounded-2xl text-lg btn-press shadow-[0_4px_20px_rgba(212,175,55,0.3)]">CONFIRM ORDER</button>
            </div>
        </div>

        <div id="item-modal" class="modal" onclick="closeItemModal(event)">
            <div class="modal-content" onclick="event.stopPropagation()">
                <input type="hidden" id="modal-item-id">
                <input type="hidden" id="modal-item-price">
                <h3 id="modal-item-name" class="text-2xl font-bold text-white mb-2 serif tracking-wide">Item Name</h3>
                <div class="text-[#D4AF37] font-black text-lg mb-6" id="modal-item-price-display">฿0.00</div>
                
                <label class="block text-xs font-bold tracking-widest text-slate-400 uppercase mb-3">Special Requests & Dietary Options</label>
                <textarea id="modal-item-notes" placeholder="e.g. No spicy, dressing on the side, allergies..." class="w-full bg-slate-800/50 text-white border border-white/10 rounded-2xl p-4 outline-none focus:border-[#D4AF37] transition mb-8 resize-none h-28 text-sm placeholder-slate-500"></textarea>
                
                <div class="flex justify-between items-center">
                    <div class="flex items-center gap-5 bg-slate-800/80 border border-white/5 rounded-2xl p-1.5 shadow-inner">
                        <button onclick="adjustModalQty(-1)" class="w-12 h-12 flex items-center justify-center text-2xl text-white btn-press hover:bg-white/5 rounded-xl">-</button>
                        <span id="modal-item-qty" class="font-black text-xl w-6 text-center text-white">1</span>
                        <button onclick="adjustModalQty(1)" class="w-12 h-12 flex items-center justify-center text-2xl text-[#D4AF37] btn-press hover:bg-[#D4AF37]/10 rounded-xl">+</button>
                    </div>
                    <button onclick="addToCartConfirm()" class="bg-gold text-dark font-black tracking-wide px-8 py-4.5 rounded-2xl btn-press shadow-[0_4px_20px_rgba(212,175,55,0.3)] text-base">Add to Order</button>
                </div>
            </div>
        </div>


        <div id="staff-view" class="hidden p-5">
            <div class="flex justify-between items-center mb-6">
                <h2 class="text-2xl font-bold gold-gradient serif">Staff Dashboard</h2>
                <span class="px-3 py-1 bg-emerald-500/10 text-emerald-400 text-xs font-bold rounded-lg border border-emerald-500/20">Online</span>
            </div>
            
            <div class="flex bg-slate-800 p-1.5 rounded-xl mb-6 border border-white/5">
                <button onclick="switchStaffTab('tables')" id="s-tab-tables" class="flex-1 py-2.5 rounded-lg bg-slate-700 text-white font-bold text-sm shadow-sm transition">Tables</button>
                <button onclick="switchStaffTab('menu')" id="s-tab-menu" class="flex-1 py-2.5 rounded-lg text-slate-400 font-bold text-sm hover:text-white transition">Menu Mgmt</button>
            </div>

            <div id="staff-tables-view">
                <div class="glass-card p-4 mb-6 flex gap-3 border-[#D4AF37]/20">
                    <input type="text" id="new-table-num" placeholder="Table No (e.g. VIP-1)" class="flex-1 bg-slate-800/80 text-white px-4 py-3 rounded-xl border border-white/10 outline-none focus:border-[#D4AF37] text-sm">
                    <button onclick="createTable()" class="bg-gold text-dark font-bold px-5 rounded-xl btn-press text-sm">Create</button>
                </div>
                <div id="staff-table-list" class="grid grid-cols-2 gap-4"></div>
            </div>

            <div id="staff-menu-view" class="hidden">
                <div class="glass-card p-6 mb-4 space-y-4">
                    <h3 class="font-bold text-[#D4AF37] uppercase tracking-wider text-xs mb-2">Add New Menu Item</h3>
                    <input type="text" id="new-m-name" placeholder="Item Name" class="w-full bg-slate-800/80 text-white p-3.5 rounded-xl border border-white/10 outline-none focus:border-[#D4AF37] text-sm">
                    <input type="number" id="new-m-price" placeholder="Price (THB)" class="w-full bg-slate-800/80 text-white p-3.5 rounded-xl border border-white/10 outline-none focus:border-[#D4AF37] text-sm">
                    <input type="text" id="new-m-cat" placeholder="Category (e.g. Signature Cocktails)" class="w-full bg-slate-800/80 text-white p-3.5 rounded-xl border border-white/10 outline-none focus:border-[#D4AF37] text-sm">
                    <input type="text" id="new-m-tags" placeholder="Tags (e.g. Vegan, Gluten-Free)" class="w-full bg-slate-800/80 text-white p-3.5 rounded-xl border border-white/10 outline-none focus:border-[#D4AF37] text-sm">
                    <textarea id="new-m-desc" placeholder="Appetizing description..." class="w-full bg-slate-800/80 text-white p-3.5 rounded-xl border border-white/10 outline-none focus:border-[#D4AF37] h-24 resize-none text-sm"></textarea>
                    <div class="bg-slate-800/50 p-3 rounded-xl border border-white/5 border-dashed">
                        <label class="block text-xs font-bold text-slate-400 mb-2">Upload Image</label>
                        <input type="file" id="new-m-img" accept="image/*" class="text-xs text-slate-400 w-full file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-xs file:font-bold file:bg-[#D4AF37]/10 file:text-[#D4AF37]" onchange="convertImg(this)">
                    </div>
                    <input type="hidden" id="new-m-img-b64">
                    <button onclick="addMenuItem()" class="w-full bg-gold text-dark font-black py-4 rounded-xl mt-4 btn-press text-sm tracking-wide">SAVE MENU ITEM</button>
                </div>
            </div>
        </div>

        <script>
            const tg = window.Telegram.WebApp;
            const initData = tg.initData;
            
            // State
            let secureToken = null;
            let currentTable = null;
            let fullMenu = [];
            let cart = [];
            
            function showToast(msg) {
                const t = document.getElementById("toast");
                t.innerText = msg; t.className = "show";
                if(tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
                setTimeout(() => { t.className = t.className.replace("show", ""); }, 3000);
            }

            async function apiFetch(url, options = {}) {
                const headers = { 'Content-Type': 'application/json' };
                if (initData) headers['X-Telegram-Init-Data'] = initData; 
                return fetch(url, { ...options, headers: { ...headers, ...options.headers }});
            }

            async function initApp() {
                const urlParams = new URLSearchParams(window.location.search);
                secureToken = urlParams.get('table_token');

                if (secureToken) {
                    // GUEST MODE (Accessed via Standard Camera QR Scan)
                    document.getElementById('guest-view').classList.remove('hidden');
                    await initGuestMode();
                } else if (initData) {
                    // STAFF MODE (Accessed via Telegram Bot)
                    tg.expand(); tg.ready();
                    document.getElementById('staff-view').classList.remove('hidden');
                    loadStaffTables();
                } else {
                    document.body.innerHTML = `<div class='flex flex-col items-center justify-center h-screen px-6 text-center bg-dark'><div class='w-20 h-20 mb-6 rounded-full border-2 border-[#D4AF37] flex items-center justify-center text-[#D4AF37] text-3xl'>📱</div><h2 class='text-2xl font-bold text-white mb-3 serif'>Welcome to Coral Beach</h2><p class='text-slate-400 text-sm'>Please scan the secure QR code on your table to access the menu and place your order.</p></div>`;
                }
            }

            // ================= GUEST LOGIC =================
            async function initGuestMode() {
                try {
                    const tRes = await apiFetch(`/api/guest/table/${secureToken}`);
                    if (!tRes.ok) throw new Error("Invalid Table");
                    currentTable = await tRes.json();
                    document.getElementById('display-table-num').innerText = currentTable.table_number;
                    
                    const mRes = await apiFetch(`/api/guest/menu`);
                    const mData = await mRes.json();
                    fullMenu = mData.items;
                    renderGuestMenu(fullMenu);
                    renderCategories(mData.categories);
                } catch (e) {
                    document.body.innerHTML = `<div class='flex flex-col items-center justify-center h-screen px-6 text-center bg-dark'><div class='w-20 h-20 mb-6 rounded-full border-2 border-red-500/50 bg-red-500/10 flex items-center justify-center text-red-500 text-3xl'>⚠️</div><h2 class='text-xl font-bold text-white mb-3'>Session Expired</h2><p class='text-slate-400 text-sm'>Your session is invalid or has expired. Please scan the QR code on your table again.</p></div>`;
                }
            }

            function renderCategories(cats) {
                if(!cats.length) cats = ["Main Course"];
                let html = `<button onclick="filterMenu('All')" class="px-5 py-2.5 rounded-full bg-[#D4AF37]/10 border border-[#D4AF37] text-[#D4AF37] whitespace-nowrap text-sm font-bold tracking-wide transition">All Items</button>`;
                cats.forEach(c => { html += `<button onclick="filterMenu('${c}')" class="px-5 py-2.5 rounded-full border border-white/10 bg-slate-800/50 text-slate-300 hover:text-white whitespace-nowrap text-sm font-bold tracking-wide transition">${c}</button>`; });
                document.getElementById('category-nav').innerHTML = html;
            }

            function filterMenu(cat) {
                if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
                const btns = document.getElementById('category-nav').children;
                for(let b of btns) {
                    if(b.innerText === cat || (cat === 'All' && b.innerText === 'All Items')) {
                        b.className = "px-5 py-2.5 rounded-full bg-[#D4AF37]/10 border border-[#D4AF37] text-[#D4AF37] whitespace-nowrap text-sm font-bold tracking-wide transition";
                    } else {
                        b.className = "px-5 py-2.5 rounded-full border border-white/10 bg-slate-800/50 text-slate-300 hover:text-white whitespace-nowrap text-sm font-bold tracking-wide transition";
                    }
                }
                const filtered = cat === 'All' ? fullMenu : fullMenu.filter(i => i.category === cat);
                renderGuestMenu(filtered);
            }

            function renderGuestMenu(items) {
                document.getElementById('guest-menu').innerHTML = items.map((i, idx) => {
                    const tagHtml = i.tags ? `<div class="text-[9px] text-[#D4AF37] font-black tracking-[0.15em] uppercase mb-1.5 bg-[#D4AF37]/10 inline-block px-2 py-0.5 rounded border border-[#D4AF37]/20">${i.tags}</div>` : '';
                    const imgHtml = i.image ? `<img src="${i.image}" class="w-28 h-28 object-cover rounded-xl shrink-0 shadow-md">` : '';
                    return `<div class="glass-card p-4 flex gap-5 items-center animate-slide-up hover:border-[#D4AF37]/30 transition" style="animation-delay: ${idx * 0.05}s">
                        <div class="flex-1">
                            ${tagHtml}
                            <h3 class="text-white font-bold text-lg leading-tight mb-1.5 serif">${i.name}</h3>
                            <p class="text-xs text-slate-400 line-clamp-2 mb-3 leading-relaxed">${i.description}</p>
                            <div class="text-[#D4AF37] font-black tracking-wide">฿${i.price.toLocaleString(undefined, {minimumFractionDigits: 2})}</div>
                        </div>
                        ${imgHtml}
                        <button onclick='openItemModal(${JSON.stringify(i).replace(/'/g, "&#39;")})' class="w-10 h-10 rounded-xl bg-gold text-dark flex items-center justify-center font-bold text-xl btn-press shrink-0 shadow-lg">+</button>
                    </div>`;
                }).join('');
            }

            function openItemModal(item) {
                if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
                document.getElementById('modal-item-id').value = item.id;
                document.getElementById('modal-item-name').innerText = item.name;
                document.getElementById('modal-item-price').value = item.price;
                document.getElementById('modal-item-price-display').innerText = `฿${item.price.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
                document.getElementById('modal-item-qty').innerText = "1";
                document.getElementById('modal-item-notes').value = "";
                document.getElementById('item-modal').classList.add('active');
            }

            function closeItemModal(e) { if(!e || e.target.id === 'item-modal') document.getElementById('item-modal').classList.remove('active'); }
            function adjustModalQty(delta) {
                if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
                let q = parseInt(document.getElementById('modal-item-qty').innerText) + delta;
                if(q > 0) document.getElementById('modal-item-qty').innerText = q;
            }

            function addToCartConfirm() {
                const id = parseInt(document.getElementById('modal-item-id').value);
                const name = document.getElementById('modal-item-name').innerText;
                const price = parseFloat(document.getElementById('modal-item-price').value);
                const qty = parseInt(document.getElementById('modal-item-qty').innerText);
                const notes = document.getElementById('modal-item-notes').value.trim();
                
                cart.push({ id, name, price, qty, notes, cart_id: Date.now() });
                
                updateCartBadge();
                closeItemModal();
                showToast("Added to order");
            }

            function updateCartBadge() {
                const b = document.getElementById('cart-badge');
                const t = cart.reduce((s, i) => s + i.qty, 0);
                b.innerText = t;
                b.classList.toggle('hidden', t === 0);
            }

            function openCart() {
                if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
                if(cart.length === 0) return showToast("Your order is empty");
                
                let total = 0;
                document.getElementById('cart-items').innerHTML = cart.map(i => {
                    total += (i.price * i.qty);
                    const notesHtml = i.notes ? `<div class="text-xs text-[#D4AF37] mt-1.5 italic bg-[#D4AF37]/5 px-2 py-1 rounded border border-[#D4AF37]/10">Note: ${i.notes}</div>` : '';
                    return `<div class="flex justify-between items-start bg-slate-800/80 p-4.5 rounded-2xl border border-white/5 shadow-sm">
                        <div class="flex-1 pr-4">
                            <div class="text-white font-bold text-sm leading-snug"><span class="text-[#D4AF37] mr-1">${i.qty}x</span> ${i.name}</div>
                            ${notesHtml}
                        </div>
                        <div class="text-right shrink-0">
                            <div class="text-white font-black tracking-wide mb-2">฿${(i.price * i.qty).toLocaleString(undefined, {minimumFractionDigits: 2})}</div>
                            <button onclick="removeCartItem(${i.cart_id})" class="text-[10px] text-red-400 font-bold uppercase tracking-widest bg-red-400/10 px-2 py-1 rounded border border-red-400/20 btn-press">Remove</button>
                        </div>
                    </div>`;
                }).join('');
                
                document.getElementById('cart-total').innerText = `฿${total.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
                document.getElementById('cart-modal').classList.add('active');
            }

            function closeCart(e) { if(!e || e.target.id === 'cart-modal') document.getElementById('cart-modal').classList.remove('active'); }
            function removeCartItem(cId) { if(tg.HapticFeedback) tg.HapticFeedback.selectionChanged(); cart = cart.filter(i => i.cart_id !== cId); openCart(); updateCartBadge(); if(cart.length===0) closeCart(); }

            async function submitOrder() {
                if (cart.length === 0) return;
                const btn = document.querySelector('#cart-modal button.bg-gold');
                btn.innerText = "SENDING ORDER..."; btn.disabled = true;
                
                try {
                    const res = await apiFetch('/api/guest/order', { method: 'POST', body: JSON.stringify({ secure_token: secureToken, cart: cart }) });
                    if (res.ok) {
                        showToast("✅ Order Sent to Kitchen!");
                        cart = []; updateCartBadge(); closeCart();
                    } else throw new Error();
                } catch(e) { showToast("⚠️ Error connecting to kitchen."); }
                finally { btn.innerText = "CONFIRM ORDER"; btn.disabled = false; }
            }

            async function requestService(type) {
                if(tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium');
                try {
                    await apiFetch('/api/guest/service', { method: 'POST', body: JSON.stringify({ secure_token: secureToken, type: type }) });
                    showToast(type === 'waiter' ? "🙋‍♂️ Waiter called. Please wait." : "💳 Bill requested. Staff will be with you shortly.");
                } catch(e) { showToast("Error connecting to staff."); }
            }

            // ================= STAFF LOGIC =================
            function switchStaffTab(tab) {
                document.getElementById('s-tab-tables').className = tab === 'tables' ? 'flex-1 py-2.5 rounded-lg bg-slate-700 text-white font-bold text-sm shadow-sm transition' : 'flex-1 py-2.5 rounded-lg text-slate-400 font-bold text-sm hover:text-white transition';
                document.getElementById('s-tab-menu').className = tab === 'menu' ? 'flex-1 py-2.5 rounded-lg bg-slate-700 text-white font-bold text-sm shadow-sm transition' : 'flex-1 py-2.5 rounded-lg text-slate-400 font-bold text-sm hover:text-white transition';
                document.getElementById('staff-tables-view').style.display = tab === 'tables' ? 'block' : 'none';
                document.getElementById('staff-menu-view').style.display = tab === 'menu' ? 'block' : 'none';
            }

            async function loadStaffTables() {
                const res = await apiFetch('/api/staff/tables');
                const tables = await res.json();
                document.getElementById('staff-table-list').innerHTML = tables.map(t => {
                    const qrLink = `${WEBAPP_URL}?table_token=${t.token}`;
                    let stColor = t.status === 'available' ? 'text-emerald-400 bg-emerald-400/10 border-emerald-400/20' : 
                                 (t.status === 'occupied' ? 'text-blue-400 bg-blue-400/10 border-blue-400/20' : 
                                 'text-red-400 bg-red-400/10 border-red-400/20 animate-pulse');
                    return `<div class="glass-card p-4.5 flex flex-col items-center text-center relative overflow-hidden">
                        ${t.status !== 'available' ? '<div class="absolute top-0 left-0 w-full h-1 bg-[#D4AF37]"></div>' : ''}
                        <div class="text-3xl font-bold text-white mb-2 serif">${t.number}</div>
                        <div class="text-[9px] font-black uppercase tracking-widest px-3 py-1 rounded-full border ${stColor} mb-4">${t.status.replace('_',' ')}</div>
                        <div class="flex gap-2 w-full mt-auto">
                            <button onclick="tg.openLink('https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=${encodeURIComponent(qrLink)}')" class="flex-1 bg-slate-700 hover:bg-slate-600 text-[10px] uppercase font-bold tracking-wide py-2.5 rounded-lg transition">Get QR</button>
                            <button onclick="clearTable(${t.id})" class="flex-1 border border-white/10 text-[10px] uppercase font-bold tracking-wide py-2.5 rounded-lg text-slate-400 hover:text-white hover:bg-white/5 transition">Clear</button>
                        </div>
                    </div>`;
                }).join('');
            }

            async function createTable() {
                const num = document.getElementById('new-table-num').value.trim();
                if(!num) return;
                await apiFetch('/api/staff/tables', { method: 'POST', body: JSON.stringify({ table_number: num }) });
                document.getElementById('new-table-num').value = '';
                loadStaffTables();
            }

            async function clearTable(id) {
                if(confirm('Reset this table to Available?')) {
                    await apiFetch(`/api/staff/tables/${id}/clear`, { method: 'POST' });
                    loadStaffTables();
                }
            }

            function convertImg(el) {
                let file = el.files[0]; if(!file) return; let reader = new FileReader();
                reader.onloadend = function(e) { 
                    let img = new Image(); img.onload = function() {
                        let canvas = document.createElement('canvas'); let ctx = canvas.getContext('2d');
                        let maxW = 600; let maxH = 600; let width = img.width; let height = img.height;
                        if (width > height) { if (width > maxW) { height *= maxW / width; width = maxW; } } else { if (height > maxH) { width *= maxH / height; height = maxH; } }
                        canvas.width = width; canvas.height = height; ctx.drawImage(img, 0, 0, width, height);
                        document.getElementById('new-m-img-b64').value = canvas.toDataURL('image/jpeg', 0.8);
                    }; img.src = e.target.result;
                }; reader.readAsDataURL(file);
            }

            async function addMenuItem() {
                const payload = {
                    name: document.getElementById('new-m-name').value,
                    price: document.getElementById('new-m-price').value,
                    category: document.getElementById('new-m-cat').value || 'Main Course',
                    tags: document.getElementById('new-m-tags').value,
                    description: document.getElementById('new-m-desc').value,
                    image: document.getElementById('new-m-img-b64').value
                };
                if(!payload.name || !payload.price) return showToast("Name and price required");
                
                const btn = document.querySelector('#staff-menu-view button');
                btn.innerText = "SAVING...";
                await apiFetch('/api/staff/menu', { method: 'POST', body: JSON.stringify(payload) });
                btn.innerText = "SAVE MENU ITEM";
                showToast("Menu Item Added");
                
                document.getElementById('new-m-name').value = '';
                document.getElementById('new-m-price').value = '';
                document.getElementById('new-m-desc').value = '';
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
