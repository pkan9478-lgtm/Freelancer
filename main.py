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
# 2. DATABASE MODELS (Production-Ready SQLite)
# ==========================================
DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR}/premium_restaurant_pro.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 15})
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
        except: pass # Ignore if staff blocked bot
        
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
        <title>Premium Dine-In</title>
        <style>
            :root { --gold: #D4AF37; --dark: #121212; --slate: #1e293b; }
            body { font-family: 'Inter', sans-serif; background-color: var(--dark); color: #f8fafc; -webkit-tap-highlight-color: transparent; }
            h1, h2, h3, .serif { font-family: 'Playfair Display', serif; }
            
            .gold-gradient { background: linear-gradient(135deg, #F3E5AB, #D4AF37, #C5A028); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .bg-gold { background: linear-gradient(135deg, #D4AF37, #C5A028); }
            
            .glass-card { background: rgba(30, 41, 59, 0.7); backdrop-filter: blur(16px); border: 1px solid rgba(255, 255, 255, 0.05); border-radius: 20px; }
            .btn-press:active { transform: scale(0.96); transition: transform 0.1s ease; }
            
            .modal { position: fixed; inset: 0; background: rgba(0,0,0,0.9); z-index: 100; display: none; flex-direction: column; justify-content: flex-end; }
            .modal.active { display: flex; animation: fadeIn 0.3s; }
            .modal-content { background: var(--slate); border-top-left-radius: 24px; border-top-right-radius: 24px; padding: 24px; border-top: 1px solid rgba(212, 175, 55, 0.3); }
            
            @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
            @keyframes slideUp { from { transform: translateY(20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
            .animate-slide-up { animation: slideUp 0.5s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
            
            #toast { visibility: hidden; background: #D4AF37; color: #000; text-align: center; border-radius: 12px; padding: 12px 20px; position: fixed; z-index: 1000; left: 50%; top: 40px; transform: translateX(-50%); font-weight: 800; font-size: 14px; box-shadow: 0 10px 25px rgba(212, 175, 55, 0.3); }
            #toast.show { visibility: visible; animation: fadeIn 0.3s, fadeIn 0.3s 3s reverse forwards; }
        </style>
    </head>
    <body class="pb-24">
        <div id="toast">Message</div>

        <div id="guest-view" class="hidden">
            <header class="p-6 pb-2 text-center">
                <h1 class="text-3xl font-bold gold-gradient mb-1">Coral Beach</h1>
                <p class="text-xs tracking-widest text-slate-400 uppercase">Restaurant & Bar</p>
                <div class="mt-4 inline-block px-4 py-1.5 rounded-full border border-[#D4AF37]/30 bg-[#D4AF37]/10 text-[#D4AF37] text-xs font-bold tracking-wider">
                    TABLE <span id="display-table-num">--</span>
                </div>
            </header>

            <div class="sticky top-0 z-40 bg-dark/90 backdrop-blur-md border-b border-white/5 py-3 px-4 flex gap-3 overflow-x-auto scrollbar-hide" id="category-nav">
                </div>

            <div id="guest-menu" class="p-4 space-y-4">
                </div>

            <div class="fixed bottom-0 w-full glass-card rounded-none border-t border-white/10 p-4 flex justify-between items-center z-50 pb-safe">
                <div class="flex gap-2">
                    <button onclick="requestService('waiter')" class="w-12 h-12 rounded-full border border-white/10 flex items-center justify-center text-xl bg-slate-800 btn-press">🙋‍♂️</button>
                    <button onclick="requestService('bill')" class="w-12 h-12 rounded-full border border-white/10 flex items-center justify-center text-xl bg-slate-800 btn-press">💳</button>
                </div>
                <button onclick="openCart()" class="bg-gold text-dark font-bold px-6 py-3 rounded-2xl flex items-center gap-3 btn-press shadow-[0_4px_20px_rgba(212,175,55,0.3)]">
                    <span>View Order</span>
                    <div id="cart-badge" class="bg-dark text-white text-[10px] w-5 h-5 rounded-full flex items-center justify-center hidden">0</div>
                </button>
            </div>
        </div>

        <div id="cart-modal" class="modal" onclick="closeCart(event)">
            <div class="modal-content" onclick="event.stopPropagation()">
                <div class="flex justify-between items-center mb-6">
                    <h2 class="text-2xl font-bold gold-gradient">Your Order</h2>
                    <button onclick="closeCart()" class="text-slate-400 text-2xl">&times;</button>
                </div>
                <div id="cart-items" class="max-h-[50vh] overflow-y-auto space-y-4 mb-6"></div>
                <div class="flex justify-between items-center border-t border-white/10 pt-4 mb-6">
                    <span class="text-slate-400 font-bold">Total</span>
                    <span id="cart-total" class="text-2xl font-bold text-white">฿0.00</span>
                </div>
                <button onclick="submitOrder()" class="w-full bg-gold text-dark font-bold py-4 rounded-2xl text-lg btn-press">Confirm Order</button>
            </div>
        </div>

        <div id="item-modal" class="modal" onclick="closeItemModal(event)">
            <div class="modal-content" onclick="event.stopPropagation()">
                <input type="hidden" id="modal-item-id">
                <input type="hidden" id="modal-item-price">
                <h3 id="modal-item-name" class="text-xl font-bold text-white mb-2">Item Name</h3>
                <p class="text-sm text-slate-400 mb-4">Special dietary requirements or preparation instructions?</p>
                <textarea id="modal-item-notes" placeholder="e.g. No spicy, dressing on the side..." class="w-full bg-slate-800 text-white border border-white/10 rounded-xl p-3 outline-none focus:border-[#D4AF37] transition mb-6 resize-none h-24"></textarea>
                <div class="flex justify-between items-center">
                    <div class="flex items-center gap-4 bg-slate-800 rounded-xl p-1">
                        <button onclick="adjustModalQty(-1)" class="w-10 h-10 flex items-center justify-center text-xl text-white">-</button>
                        <span id="modal-item-qty" class="font-bold text-lg w-4 text-center">1</span>
                        <button onclick="adjustModalQty(1)" class="w-10 h-10 flex items-center justify-center text-xl text-[#D4AF37]">+</button>
                    </div>
                    <button onclick="addToCartConfirm()" class="bg-gold text-dark font-bold px-8 py-3.5 rounded-xl btn-press">Add to Order</button>
                </div>
            </div>
        </div>


        <div id="staff-view" class="hidden p-4">
            <h2 class="text-2xl font-bold gold-gradient mb-6">Staff Dashboard</h2>
            
            <div class="flex bg-slate-800 p-1 rounded-xl mb-6">
                <button onclick="switchStaffTab('tables')" id="s-tab-tables" class="flex-1 py-2 rounded-lg bg-slate-700 text-white font-bold text-sm">Tables</button>
                <button onclick="switchStaffTab('menu')" id="s-tab-menu" class="flex-1 py-2 rounded-lg text-slate-400 font-bold text-sm">Menu Mgmt</button>
            </div>

            <div id="staff-tables-view">
                <div class="glass-card p-4 mb-4 flex gap-2">
                    <input type="text" id="new-table-num" placeholder="Table No (e.g. T-01)" class="flex-1 bg-slate-800 text-white px-4 py-2 rounded-lg border border-white/10 outline-none">
                    <button onclick="createTable()" class="bg-gold text-dark font-bold px-4 rounded-lg btn-press">Add</button>
                </div>
                <div id="staff-table-list" class="grid grid-cols-2 gap-3"></div>
            </div>

            <div id="staff-menu-view" class="hidden">
                <div class="glass-card p-5 mb-4 space-y-3">
                    <h3 class="font-bold text-[#D4AF37]">Add Menu Item</h3>
                    <input type="text" id="new-m-name" placeholder="Name" class="w-full bg-slate-800 text-white p-3 rounded-lg border border-white/10 outline-none">
                    <input type="number" id="new-m-price" placeholder="Price (THB)" class="w-full bg-slate-800 text-white p-3 rounded-lg border border-white/10 outline-none">
                    <input type="text" id="new-m-cat" placeholder="Category" class="w-full bg-slate-800 text-white p-3 rounded-lg border border-white/10 outline-none">
                    <input type="text" id="new-m-tags" placeholder="Tags (Vegan, GF)" class="w-full bg-slate-800 text-white p-3 rounded-lg border border-white/10 outline-none">
                    <textarea id="new-m-desc" placeholder="Description" class="w-full bg-slate-800 text-white p-3 rounded-lg border border-white/10 outline-none h-20"></textarea>
                    <input type="file" id="new-m-img" accept="image/*" class="text-xs text-slate-400 w-full" onchange="convertImg(this)">
                    <input type="hidden" id="new-m-img-b64">
                    <button onclick="addMenuItem()" class="w-full bg-gold text-dark font-bold py-3 rounded-lg mt-2 btn-press">Save Item</button>
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
                if (initData) headers['X-Telegram-Init-Data'] = initData; // Only if in Telegram WebApp
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
                    document.body.innerHTML = "<div class='p-10 text-center text-slate-400'>Please scan a Table QR code to order.</div>";
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
                    document.body.innerHTML = "<div class='p-10 text-center text-red-400 text-xl font-bold'>Session Expired or Invalid QR.<br>Please scan the table QR again.</div>";
                }
            }

            function renderCategories(cats) {
                if(!cats.length) cats = ["Main Course"];
                let html = `<button onclick="filterMenu('All')" class="px-5 py-2 rounded-full border border-[#D4AF37] text-[#D4AF37] whitespace-nowrap text-sm font-bold">All</button>`;
                cats.forEach(c => { html += `<button onclick="filterMenu('${c}')" class="px-5 py-2 rounded-full border border-white/10 text-slate-400 whitespace-nowrap text-sm font-bold">${c}</button>`; });
                document.getElementById('category-nav').innerHTML = html;
            }

            function filterMenu(cat) {
                // UI Highlight
                const btns = document.getElementById('category-nav').children;
                for(let b of btns) {
                    if(b.innerText === cat || (cat === 'All' && b.innerText === 'All')) {
                        b.className = "px-5 py-2 rounded-full border border-[#D4AF37] text-[#D4AF37] whitespace-nowrap text-sm font-bold";
                    } else {
                        b.className = "px-5 py-2 rounded-full border border-white/10 text-slate-400 whitespace-nowrap text-sm font-bold";
                    }
                }
                const filtered = cat === 'All' ? fullMenu : fullMenu.filter(i => i.category === cat);
                renderGuestMenu(filtered);
            }

            function renderGuestMenu(items) {
                document.getElementById('guest-menu').innerHTML = items.map((i, idx) => {
                    const tagHtml = i.tags ? `<div class="text-[10px] text-[#D4AF37] font-bold tracking-wider uppercase mb-1">${i.tags}</div>` : '';
                    const imgHtml = i.image ? `<img src="${i.image}" class="w-24 h-24 object-cover rounded-xl shrink-0">` : '';
                    return `<div class="glass-card p-4 flex gap-4 items-center animate-slide-up" style="animation-delay: ${idx * 0.05}s">
                        <div class="flex-1">
                            ${tagHtml}
                            <h3 class="text-white font-bold text-lg leading-tight mb-1">${i.name}</h3>
                            <p class="text-xs text-slate-400 line-clamp-2 mb-2">${i.description}</p>
                            <div class="text-[#D4AF37] font-bold">฿${i.price.toLocaleString(undefined, {minimumFractionDigits: 2})}</div>
                        </div>
                        ${imgHtml}
                        <button onclick='openItemModal(${JSON.stringify(i).replace(/'/g, "&#39;")})' class="w-8 h-8 rounded-full bg-[#D4AF37] text-dark flex items-center justify-center font-bold text-xl btn-press shrink-0">+</button>
                    </div>`;
                }).join('');
            }

            function openItemModal(item) {
                document.getElementById('modal-item-id').value = item.id;
                document.getElementById('modal-item-name').innerText = item.name;
                document.getElementById('modal-item-price').value = item.price;
                document.getElementById('modal-item-qty').innerText = "1";
                document.getElementById('modal-item-notes').value = "";
                document.getElementById('item-modal').classList.add('active');
            }

            function closeItemModal(e) { if(!e || e.target.id === 'item-modal') document.getElementById('item-modal').classList.remove('active'); }
            function adjustModalQty(delta) {
                let q = parseInt(document.getElementById('modal-item-qty').innerText) + delta;
                if(q > 0) document.getElementById('modal-item-qty').innerText = q;
            }

            function addToCartConfirm() {
                const id = parseInt(document.getElementById('modal-item-id').value);
                const name = document.getElementById('modal-item-name').innerText;
                const price = parseFloat(document.getElementById('modal-item-price').value);
                const qty = parseInt(document.getElementById('modal-item-qty').innerText);
                const notes = document.getElementById('modal-item-notes').value.trim();
                
                // For restaurant, we treat different notes as separate line items
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
                if(cart.length === 0) return showToast("Your order is empty");
                
                let total = 0;
                document.getElementById('cart-items').innerHTML = cart.map(i => {
                    total += (i.price * i.qty);
                    const notesHtml = i.notes ? `<div class="text-xs text-[#D4AF37] mt-1 italic">Note: ${i.notes}</div>` : '';
                    return `<div class="flex justify-between items-center bg-slate-800 p-4 rounded-xl border border-white/5">
                        <div class="flex-1 pr-4">
                            <div class="text-white font-bold">${i.qty}x ${i.name}</div>
                            ${notesHtml}
                        </div>
                        <div class="text-right">
                            <div class="text-white font-bold mb-1">฿${(i.price * i.qty).toLocaleString(undefined, {minimumFractionDigits: 2})}</div>
                            <button onclick="removeCartItem(${i.cart_id})" class="text-xs text-red-400 font-bold uppercase tracking-wide">Remove</button>
                        </div>
                    </div>`;
                }).join('');
                
                document.getElementById('cart-total').innerText = `฿${total.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
                document.getElementById('cart-modal').classList.add('active');
            }

            function closeCart(e) { if(!e || e.target.id === 'cart-modal') document.getElementById('cart-modal').classList.remove('active'); }
            function removeCartItem(cId) { cart = cart.filter(i => i.cart_id !== cId); openCart(); updateCartBadge(); if(cart.length===0) closeCart(); }

            async function submitOrder() {
                if (cart.length === 0) return;
                const btn = document.querySelector('#cart-modal button.bg-gold');
                btn.innerText = "Sending..."; btn.disabled = true;
                
                try {
                    const res = await apiFetch('/api/guest/order', { method: 'POST', body: JSON.stringify({ secure_token: secureToken, cart: cart }) });
                    if (res.ok) {
                        showToast("✅ Order Sent to Kitchen!");
                        cart = []; updateCartBadge(); closeCart();
                    } else throw new Error();
                } catch(e) { showToast("⚠️ Error sending order."); }
                finally { btn.innerText = "Confirm Order"; btn.disabled = false; }
            }

            async function requestService(type) {
                try {
                    await apiFetch('/api/guest/service', { method: 'POST', body: JSON.stringify({ secure_token: secureToken, type: type }) });
                    showToast(type === 'waiter' ? "🙋‍♂️ Waiter called. Please wait." : "💳 Bill requested. Staff will be with you shortly.");
                } catch(e) { showToast("Error connecting to staff."); }
            }

            // ================= STAFF LOGIC =================
            function switchStaffTab(tab) {
                document.getElementById('s-tab-tables').className = tab === 'tables' ? 'flex-1 py-2 rounded-lg bg-slate-700 text-white font-bold text-sm' : 'flex-1 py-2 rounded-lg text-slate-400 font-bold text-sm';
                document.getElementById('s-tab-menu').className = tab === 'menu' ? 'flex-1 py-2 rounded-lg bg-slate-700 text-white font-bold text-sm' : 'flex-1 py-2 rounded-lg text-slate-400 font-bold text-sm';
                document.getElementById('staff-tables-view').style.display = tab === 'tables' ? 'block' : 'none';
                document.getElementById('staff-menu-view').style.display = tab === 'menu' ? 'block' : 'none';
            }

            async function loadStaffTables() {
                const res = await apiFetch('/api/staff/tables');
                const tables = await res.json();
                document.getElementById('staff-table-list').innerHTML = tables.map(t => {
                    const qrLink = `${WEBAPP_URL}?table_token=${t.token}`;
                    let stColor = t.status === 'available' ? 'text-emerald-400' : (t.status === 'occupied' ? 'text-blue-400' : 'text-red-400');
                    return `<div class="glass-card p-4 flex flex-col items-center text-center">
                        <div class="text-2xl font-bold text-white mb-1">${t.number}</div>
                        <div class="text-xs font-bold uppercase ${stColor} mb-3">${t.status.replace('_',' ')}</div>
                        <div class="flex gap-2 w-full mt-auto">
                            <button onclick="tg.openLink('https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=${encodeURIComponent(qrLink)}')" class="flex-1 bg-slate-700 text-xs py-2 rounded-lg font-bold">QR</button>
                            <button onclick="clearTable(${t.id})" class="flex-1 border border-white/10 text-xs py-2 rounded-lg text-slate-400 hover:text-white">Clear</button>
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
                        let maxW = 500; let maxH = 500; let width = img.width; let height = img.height;
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
                btn.innerText = "Saving...";
                await apiFetch('/api/staff/menu', { method: 'POST', body: JSON.stringify(payload) });
                btn.innerText = "Save Item";
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
