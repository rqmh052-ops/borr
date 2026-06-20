import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
import datetime
import time
import os
import hashlib
import hmac
import secrets
import json
import shutil
import threading

# ======================= الثوابت =======================
BOT_TOKEN = "8737177889:AAGnnxZq9Yyptc1cdLTpEv5FoNbT5Jn8SQY"
ADMIN_ID = 8287678319
FORCE_SUB_CHANNEL_ID = -1003816376312    # قناة الاشتراك الإجباري
FORCE_SUB_CHANNEL_URL = "https://t.me/Bayan_x777"
DB_CHANNEL_ID = -1004325834135           # ← غيّر هذا لـ ID قناة قاعدة البيانات الخاصة بك
# =====================================================

bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}
pending_app_messages = {}   # يخزن مؤقتاً رسالة الملف الأصلية من الإدمن

# مفتاح سري للتشفير الداخلي - لا يُشارك أبداً
SECRET_KEY = secrets.token_hex(32)

# ======================= نظام التشفير الداخلي =======================
def generate_app_code(message_id: int) -> str:
    """
    ينشئ كوداً داخلياً قصيراً من 7 أرقام مرتبطاً بـ message_id.
    الكود لا يمت بصلة ظاهرية للـ message_id.
    """
    raw = f"{SECRET_KEY}:{message_id}:APP"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    # نأخذ 7 أرقام عشوائية من الـ hash
    num = int(digest[:14], 16) % 9_000_000 + 1_000_000
    return str(num)

def decode_app_code(code: str) -> int | None:
    """
    يحوّل الكود القصير إلى message_id من قاعدة البيانات المحلية.
    """
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT channel_msg_id FROM app_codes WHERE app_code=?", (code,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def generate_user_token(user_id: int, app_code: str) -> str:
    """
    ينشئ token مؤقت (60 ثانية) يدمج user_id + app_code.
    هذا الـ token هو الوحيد الذي يُستخدم لتحديد أي تطبيق لأي مستخدم.
    """
    expires = int(time.time()) + 60
    raw = f"{SECRET_KEY}:{user_id}:{app_code}:{expires}"
    sig = hmac.new(SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]
    # نخزن مؤقتاً في الذاكرة
    pending_tokens[f"{user_id}:{app_code}"] = {
        "expires": expires,
        "sig": sig,
        "app_code": app_code,
        "user_id": user_id
    }
    return f"{user_id}:{app_code}:{sig}"

def validate_token(token: str, user_id: int) -> str | None:
    """
    يتحقق من الـ token ويعيد app_code إذا كان صالحاً.
    يُحذف الـ token فور استخدامه (one-time use).
    """
    parts = token.split(":")
    if len(parts) != 3:
        return None
    tid, app_code, sig = parts
    if int(tid) != user_id:
        return None
    key = f"{user_id}:{app_code}"
    data = pending_tokens.get(key)
    if not data:
        return None
    if time.time() > data["expires"]:
        pending_tokens.pop(key, None)
        return None
    if data["sig"] != sig:
        return None
    pending_tokens.pop(key, None)  # استخدام لمرة واحدة
    return app_code

# تخزين مؤقت في الذاكرة للـ tokens
pending_tokens = {}

# ======================= قاعدة البيانات =======================
DB_PATH = "bot_data.db"
BACKUP_DIR = "backups"

def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")   # للحماية من الانهيار
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    conn = db_conn()
    c = conn.cursor()

    # ===================== جداول المستخدمين (محمية في SQLite) =====================
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        username TEXT,
        balance REAL DEFAULT 0,
        is_vip INTEGER DEFAULT 0,
        vip_activated_date TEXT,
        referrer_id INTEGER,
        join_date TEXT,
        is_banned INTEGER DEFAULT 0
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER NOT NULL,
        referred_id INTEGER NOT NULL,
        status TEXT DEFAULT 'pending',
        created_at TEXT,
        completed_at TEXT,
        channel_checked_date TEXT,
        UNIQUE(referrer_id, referred_id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS banned_pairs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user1_id INTEGER,
        user2_id INTEGER
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS user_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        type TEXT,
        app_name TEXT,
        description TEXT,
        file_id TEXT,
        status TEXT DEFAULT 'pending',
        admin_feedback TEXT,
        created_at TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS admin_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER,
        action TEXT,
        details TEXT,
        created_at TEXT
    )''')

    # ===================== جدول قاعدة بيانات التطبيقات (مرتبطة بالقناة) =====================
    # هنا يُخزن فقط: الكود المشفر + message_id في القناة + بيانات بسيطة
    c.execute('''CREATE TABLE IF NOT EXISTS app_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        app_code TEXT UNIQUE NOT NULL,       -- الكود القصير 7 أرقام
        channel_msg_id INTEGER UNIQUE NOT NULL, -- رقم الرسالة في قناة DB
        category TEXT DEFAULT 'عام',
        is_vip INTEGER DEFAULT 0,
        name TEXT,                            -- للبحث فقط
        download_count INTEGER DEFAULT 0,
        added_date TEXT
    )''')

    # ===================== التصنيفات =====================
    c.execute('''CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        admin_id INTEGER,
        created_at TEXT
    )''')

    # الإعدادات الافتراضية
    defaults = {
        'referrals_for_feature': '2',
        'referrals_for_vip': '10',
        'referral_expire_days': '4',
        'grace_period_hours': '24',
        'maintenance_mode': '0'
    }
    for key, val in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))

    conn.commit()
    conn.close()

init_db()

# ======================= نسخ احتياطي تلقائي =======================
def auto_backup():
    """نسخة احتياطية كل 6 ساعات"""
    while True:
        time.sleep(6 * 3600)
        try:
            ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_path = os.path.join(BACKUP_DIR, f"backup_{ts}.db")
            # نسخ آمن باستخدام SQLite backup API
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect(backup_path)
            src.backup(dst)
            dst.close()
            src.close()
            # إرسال للإدمن
            with open(backup_path, 'rb') as f:
                bot.send_document(ADMIN_ID, f, caption=f"💾 نسخة احتياطية تلقائية\n🕐 {ts}")
            # احتفظ بآخر 10 نسخ فقط
            backups = sorted([
                os.path.join(BACKUP_DIR, f)
                for f in os.listdir(BACKUP_DIR)
                if f.endswith('.db')
            ])
            for old in backups[:-10]:
                os.remove(old)
        except Exception as e:
            print(f"⚠️ خطأ في النسخ الاحتياطي: {e}")

threading.Thread(target=auto_backup, daemon=True).start()

# ======================= دوال مساعدة =======================
def get_setting(key):
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE settings SET value=? WHERE key=?", (value, key))
    conn.commit()
    conn.close()

def log_admin_action(admin_id, action, details=""):
    conn = db_conn()
    c = conn.cursor()
    c.execute("INSERT INTO admin_logs (admin_id, action, details, created_at) VALUES (?, ?, ?, ?)",
              (admin_id, action, details, datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()

def is_admin(user_id):
    return user_id == ADMIN_ID

def get_user(tg_id):
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    user = c.fetchone()
    conn.close()
    return user

def register_user(tg_id, username, referrer_id=None):
    conn = db_conn()
    c = conn.cursor()
    if not get_user(tg_id):
        try:
            c.execute("INSERT INTO users (tg_id, username, referrer_id, join_date) VALUES (?, ?, ?, ?)",
                      (tg_id, username, referrer_id, datetime.datetime.now().isoformat()))
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            conn.close()
            return False
    conn.close()
    return False

def update_balance(tg_id, amount):
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE tg_id=?", (amount, tg_id))
    conn.commit()
    conn.close()

def set_vip(tg_id):
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET is_vip=1, vip_activated_date=? WHERE tg_id=?",
              (datetime.datetime.now().isoformat(), tg_id))
    conn.commit()
    conn.close()

def add_referral(referrer_id, referred_id):
    conn = db_conn()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO referrals (referrer_id, referred_id, status, created_at) VALUES (?, ?, ?, ?)",
                  (referrer_id, referred_id, 'pending', datetime.datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False

def check_subscription(tg_id):
    try:
        member = bot.get_chat_member(FORCE_SUB_CHANNEL_ID, tg_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return False

def process_pending_referrals():
    conn = db_conn()
    c = conn.cursor()
    now = datetime.datetime.now()
    expire_days = int(get_setting('referral_expire_days') or 4)
    grace_hours = int(get_setting('grace_period_hours') or 24)

    c.execute("SELECT id, referrer_id, referred_id, created_at, channel_checked_date FROM referrals WHERE status='pending'")
    pendings = c.fetchall()
    for ref_id, referrer_id, referred_id, created_at, last_check in pendings:
        created_time = datetime.datetime.fromisoformat(created_at)
        days_passed = (now - created_time).days

        if not check_subscription(referred_id):
            if last_check:
                last_time = datetime.datetime.fromisoformat(last_check)
                if (now - last_time).total_seconds() > grace_hours * 3600:
                    c.execute("UPDATE referrals SET status='cancelled' WHERE id=?", (ref_id,))
                    conn.commit()
            else:
                c.execute("UPDATE referrals SET channel_checked_date=? WHERE id=?", (now.isoformat(), ref_id))
                conn.commit()
        else:
            if days_passed >= expire_days:
                c.execute("UPDATE referrals SET status='completed', completed_at=? WHERE id=?",
                          (now.isoformat(), ref_id))
                update_balance(referrer_id, 0.5)
                c.execute("INSERT INTO banned_pairs (user1_id, user2_id) VALUES (?, ?)", (referrer_id, referred_id))
                conn.commit()
    conn.close()

def get_categories():
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT id, name FROM categories ORDER BY name")
    data = c.fetchall()
    conn.close()
    return data

def get_category_name(cat_id):
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT name FROM categories WHERE id=?", (cat_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

# ======================= دوال قاعدة بيانات القناة =======================

def register_app_in_channel(channel_msg_id: int, name: str, category: str = "عام", is_vip: bool = False) -> str:
    """
    يُسجّل رسالة القناة في جدول app_codes وينشئ كوداً قصيراً.
    يُعيد الكود القصير المُنشأ.
    """
    app_code = generate_app_code(channel_msg_id)
    conn = db_conn()
    c = conn.cursor()
    # تأكد من عدم التكرار
    c.execute("SELECT app_code FROM app_codes WHERE channel_msg_id=?", (channel_msg_id,))
    existing = c.fetchone()
    if existing:
        conn.close()
        return existing[0]
    try:
        c.execute(
            "INSERT INTO app_codes (app_code, channel_msg_id, category, is_vip, name, added_date) VALUES (?, ?, ?, ?, ?, ?)",
            (app_code, channel_msg_id, category, 1 if is_vip else 0, name, datetime.datetime.now().isoformat())
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # إذا تعارض الكود نادراً، أنشئ واحداً مختلفاً
        app_code = str(int(app_code) + 1)
        c.execute(
            "INSERT OR IGNORE INTO app_codes (app_code, channel_msg_id, category, is_vip, name, added_date) VALUES (?, ?, ?, ?, ?, ?)",
            (app_code, channel_msg_id, category, 1 if is_vip else 0, name, datetime.datetime.now().isoformat())
        )
        conn.commit()
    conn.close()
    return app_code

def get_apps_by_category(category: str, is_vip: bool = False, page: int = 1, per_page: int = 5):
    conn = db_conn()
    c = conn.cursor()
    offset = (page - 1) * per_page
    c.execute(
        "SELECT app_code, name, download_count FROM app_codes WHERE category=? AND is_vip=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (category, 1 if is_vip else 0, per_page, offset)
    )
    apps = c.fetchall()
    c.execute("SELECT COUNT(*) FROM app_codes WHERE category=? AND is_vip=?", (category, 1 if is_vip else 0))
    total = c.fetchone()[0]
    conn.close()
    return apps, total

def get_all_vip_apps(page: int = 1, per_page: int = 5):
    conn = db_conn()
    c = conn.cursor()
    offset = (page - 1) * per_page
    c.execute(
        "SELECT app_code, name, download_count FROM app_codes WHERE is_vip=1 ORDER BY id DESC LIMIT ? OFFSET ?",
        (per_page, offset)
    )
    apps = c.fetchall()
    c.execute("SELECT COUNT(*) FROM app_codes WHERE is_vip=1")
    total = c.fetchone()[0]
    conn.close()
    return apps, total

def get_app_info(app_code: str):
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT app_code, channel_msg_id, category, is_vip, name, download_count FROM app_codes WHERE app_code=?", (app_code,))
    row = c.fetchone()
    conn.close()
    return row

def increment_download(app_code: str):
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE app_codes SET download_count = download_count + 1 WHERE app_code=?", (app_code,))
    conn.commit()
    conn.close()

def delete_app_by_code(app_code: str):
    conn = db_conn()
    c = conn.cursor()
    c.execute("DELETE FROM app_codes WHERE app_code=?", (app_code,))
    conn.commit()
    conn.close()

def search_apps(query: str, page: int = 1, per_page: int = 10):
    conn = db_conn()
    c = conn.cursor()
    offset = (page - 1) * per_page
    c.execute(
        "SELECT app_code, name, download_count FROM app_codes WHERE name LIKE ? LIMIT ? OFFSET ?",
        (f"%{query}%", per_page, offset)
    )
    apps = c.fetchall()
    c.execute("SELECT COUNT(*) FROM app_codes WHERE name LIKE ?", (f"%{query}%",))
    total = c.fetchone()[0]
    conn.close()
    return apps, total

def deliver_app_to_user(app_code: str, tg_id: int) -> bool:
    """
    الدالة الجوهرية: تجلب رقم الرسالة من القناة وتحولها للمستخدم.
    كل العملية تصير في الخلفية.
    """
    info = get_app_info(app_code)
    if not info:
        return False
    _, channel_msg_id, category, is_vip, name, downloads = info
    try:
        # forward من قناة قاعدة البيانات مباشرة للمستخدم
        bot.forward_message(
            chat_id=tg_id,
            from_chat_id=DB_CHANNEL_ID,
            message_id=channel_msg_id
        )
        increment_download(app_code)
        return True
    except Exception as e:
        bot.send_message(ADMIN_ID, f"⚠️ خطأ في تحويل رسالة app_code={app_code}: {e}")
        return False

# ======================= إحصائيات =======================
def get_stats():
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM app_codes WHERE is_vip=0")
    total_apps = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM app_codes WHERE is_vip=1")
    total_vip = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM user_requests WHERE status='pending'")
    pending_reqs = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM referrals WHERE status='completed'")
    total_refs = c.fetchone()[0]
    c.execute("SELECT SUM(balance) FROM users")
    total_balance = c.fetchone()[0] or 0
    conn.close()
    return total_users, total_apps, total_vip, pending_reqs, total_refs, total_balance

# ======================= واجهة الأزرار =======================
def main_menu_keyboard(tg_id):
    keyboard = InlineKeyboardMarkup(row_width=2)
    buttons = [
        InlineKeyboardButton("📂 التطبيقات", callback_data="show_categories"),
        InlineKeyboardButton("👑 VIP", callback_data="show_vip"),
        InlineKeyboardButton("💰 رصيدي", callback_data="show_balance"),
        InlineKeyboardButton("🔗 رابط الدعوة", callback_data="get_referral_link"),
        InlineKeyboardButton("📨 طلب كسر / رفع", callback_data="make_request"),
        InlineKeyboardButton("🔍 بحث", callback_data="search_apps"),
    ]
    keyboard.add(*buttons[:2])
    keyboard.add(*buttons[2:4])
    keyboard.add(buttons[4])
    keyboard.add(buttons[5])
    if is_admin(tg_id):
        keyboard.add(InlineKeyboardButton("⚙️ لوحة الإدارة", callback_data="admin_panel"))
    return keyboard

# ======================= معالج الأزرار المركزي =======================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    try:
        data = call.data
        tg_id = call.from_user.id
        message_id = call.message.message_id
        chat_id = call.message.chat.id

        # وضع الصيانة
        if get_setting('maintenance_mode') == '1' and not is_admin(tg_id) and data not in ['check_subscription', 'dummy']:
            bot.answer_callback_query(call.id, "🔧 البوت في وضع الصيانة.", show_alert=True)
            return

        # التحقق من الاشتراك
        if data not in ['check_subscription', 'dummy']:
            if not check_subscription(tg_id):
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("📢 اشترك في القناة", url=FORCE_SUB_CHANNEL_URL))
                keyboard.add(InlineKeyboardButton("🔄 تأكد", callback_data="check_subscription"))
                bot.edit_message_text("⚠️ يجب الاشتراك في القناة:", chat_id, message_id, reply_markup=keyboard)
                return

        # ========== القائمة الرئيسية ==========
        if data == "main_menu":
            bot.edit_message_text("🏠 اختر من القائمة:", chat_id, message_id, reply_markup=main_menu_keyboard(tg_id))

        elif data == "check_subscription":
            if check_subscription(tg_id):
                bot.edit_message_text("✅ تم التأكيد!", chat_id, message_id)
                bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
            else:
                bot.answer_callback_query(call.id, "❌ لم تشترك بعد.", show_alert=True)

        elif data == "search_apps":
            user_states[tg_id] = "waiting_search_query"
            bot.edit_message_text("🔍 اكتب كلمة البحث:", chat_id, message_id)

        # ========== التصنيفات ==========
        elif data == "show_categories":
            categories = get_categories()
            keyboard = InlineKeyboardMarkup(row_width=1)
            for cat_id, name in categories:
                keyboard.add(InlineKeyboardButton(f"📁 {name}", callback_data=f"catv_{cat_id}_p_1"))
            if is_admin(tg_id):
                keyboard.add(InlineKeyboardButton("➕ إضافة تصنيف", callback_data="add_category"))
                keyboard.add(InlineKeyboardButton("🗑️ حذف تصنيف", callback_data="delete_category_menu"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text("📂 اختر التصنيف:", chat_id, message_id, reply_markup=keyboard)

        elif data == "add_category":
            if not is_admin(tg_id):
                return
            user_states[tg_id] = "waiting_category_name"
            bot.edit_message_text("✏️ أرسل اسم التصنيف الجديد:", chat_id, message_id)

        elif data == "delete_category_menu":
            if not is_admin(tg_id):
                return
            categories = get_categories()
            if not categories:
                bot.answer_callback_query(call.id, "لا توجد تصنيفات.")
                return
            keyboard = InlineKeyboardMarkup(row_width=1)
            for cat_id, name in categories:
                keyboard.add(InlineKeyboardButton(f"🗑️ {name}", callback_data=f"delete_cat_{cat_id}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_categories"))
            bot.edit_message_text("اختر تصنيفاً لحذفه:", chat_id, message_id, reply_markup=keyboard)

        elif data.startswith("delete_cat_"):
            if not is_admin(tg_id):
                return
            cat_id = int(data.split("_")[2])
            cat_name = get_category_name(cat_id)
            conn = db_conn()
            c = conn.cursor()
            # تحديث تطبيقات هذا التصنيف لتصبح "عام" بدلاً من حذفها
            c.execute("UPDATE app_codes SET category='عام' WHERE category=(SELECT name FROM categories WHERE id=?)", (cat_id,))
            c.execute("DELETE FROM categories WHERE id=?", (cat_id,))
            conn.commit()
            conn.close()
            log_admin_action(tg_id, "delete_category", f"حذف تصنيف: {cat_name}")
            bot.answer_callback_query(call.id, f"✅ تم حذف {cat_name}")
            categories = get_categories()
            keyboard = InlineKeyboardMarkup(row_width=1)
            for cid, name in categories:
                keyboard.add(InlineKeyboardButton(f"📁 {name}", callback_data=f"catv_{cid}_p_1"))
            if is_admin(tg_id):
                keyboard.add(InlineKeyboardButton("➕ إضافة تصنيف", callback_data="add_category"))
                keyboard.add(InlineKeyboardButton("🗑️ حذف تصنيف", callback_data="delete_category_menu"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text("📂 اختر التصنيف:", chat_id, message_id, reply_markup=keyboard)

        # ========== عرض تطبيقات تصنيف (من قناة DB) ==========
        elif data.startswith("catv_"):
            # Format: catv_{cat_id}_p_{page}
            parts = data.split("_")
            cat_id = int(parts[1])
            page = int(parts[3]) if len(parts) >= 4 else 1
            cat_name = get_category_name(cat_id) or "تصنيف"

            apps, total = get_apps_by_category(cat_name, is_vip=False, page=page)
            keyboard = InlineKeyboardMarkup(row_width=1)

            # تنقل الصفحات
            nav = []
            if page > 1:
                nav.append(InlineKeyboardButton("⏪", callback_data=f"catv_{cat_id}_p_{page-1}"))
            if page * 5 < total:
                nav.append(InlineKeyboardButton("⏩", callback_data=f"catv_{cat_id}_p_{page+1}"))
            if nav:
                keyboard.add(*nav)

            for app_code, name, downloads in apps:
                keyboard.add(InlineKeyboardButton(
                    f"📱 {name} ({downloads}⬇️)",
                    callback_data=f"appv_{app_code}"
                ))

            if not apps:
                keyboard.add(InlineKeyboardButton("📭 لا توجد تطبيقات", callback_data="dummy"))

            if is_admin(tg_id):
                keyboard.add(InlineKeyboardButton("➕ إضافة تطبيق", callback_data=f"addapp_{cat_id}"))
                keyboard.add(InlineKeyboardButton("🗑️ حذف تطبيق", callback_data=f"delappmenu_{cat_id}"))

            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_categories"))
            bot.edit_message_text(
                f"📂 {cat_name} — صفحة {page} ({total} تطبيق)",
                chat_id, message_id, reply_markup=keyboard
            )

        # ========== عرض التطبيق مع زر التحميل ==========
        elif data.startswith("appv_"):
            app_code = data[5:]  # بعد "appv_"
            info = get_app_info(app_code)
            if not info:
                bot.answer_callback_query(call.id, "التطبيق غير موجود.")
                return
            _, channel_msg_id, category, is_vip, name, downloads = info

            # نُنشئ token مؤقت مدمج: user_id + app_code
            token = generate_user_token(tg_id, app_code)
            safe_token = token.replace(":", "_")  # نجعله آمناً للـ callback_data

            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton(
                "📥 تحميل",
                callback_data=f"dl_{safe_token}"
            ))
            if is_admin(tg_id):
                keyboard.add(InlineKeyboardButton("🗑️ حذف", callback_data=f"delapp_{app_code}"))

            # رجوع لتصنيف التطبيق
            categories = get_categories()
            cat_id_back = next((cid for cid, cname in categories if cname == category), None)
            if cat_id_back:
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data=f"catv_{cat_id_back}_p_1"))
            else:
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_categories"))

            label = "👑 VIP" if is_vip else "📱"
            bot.edit_message_text(
                f"{label} *{name}*\n⬇️ {downloads} تحميل",
                chat_id, message_id,
                parse_mode='Markdown',
                reply_markup=keyboard
            )

        # ========== زر التحميل الفعلي (مع Token) ==========
        elif data.startswith("dl_"):
            safe_token = data[3:]
            token = safe_token.replace("_", ":", 2)  # نعيد ":" للمواضع الصحيحة

            app_code = validate_token(token, tg_id)
            if not app_code:
                bot.answer_callback_query(call.id, "⏱️ انتهت صلاحية الزر، اضغط على التطبيق مرة أخرى.", show_alert=True)
                return

            success = deliver_app_to_user(app_code, tg_id)
            if success:
                bot.answer_callback_query(call.id, "✅ تم الإرسال!")
            else:
                bot.answer_callback_query(call.id, "❌ خطأ في الإرسال، تم إبلاغ الإدمن.", show_alert=True)

        # ========== حذف تطبيق ==========
        elif data.startswith("delapp_"):
            if not is_admin(tg_id):
                return
            app_code = data[7:]
            info = get_app_info(app_code)
            if info:
                delete_app_by_code(app_code)
                log_admin_action(tg_id, "delete_app", f"حذف app_code={app_code}, name={info[4]}")
                bot.answer_callback_query(call.id, f"✅ تم حذف {info[4]}")
            bot.edit_message_text("✅ تم الحذف.", chat_id, message_id,
                                  reply_markup=InlineKeyboardMarkup().add(
                                      InlineKeyboardButton("🔙 رجوع", callback_data="show_categories")))

        elif data.startswith("delappmenu_"):
            if not is_admin(tg_id):
                return
            cat_id = int(data.split("_")[1])
            cat_name = get_category_name(cat_id) or "عام"
            apps, _ = get_apps_by_category(cat_name, is_vip=False, page=1, per_page=20)
            if not apps:
                bot.answer_callback_query(call.id, "لا توجد تطبيقات.")
                return
            keyboard = InlineKeyboardMarkup(row_width=1)
            for app_code, name, _ in apps:
                keyboard.add(InlineKeyboardButton(f"🗑️ {name}", callback_data=f"delapp_{app_code}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data=f"catv_{cat_id}_p_1"))
            bot.edit_message_text("اختر تطبيقاً لحذفه:", chat_id, message_id, reply_markup=keyboard)

        elif data.startswith("addapp_"):
            if not is_admin(tg_id):
                return
            cat_id = int(data.split("_")[1])
            user_states[tg_id] = f"waiting_app_file|{cat_id}|0"
            bot.edit_message_text(
                "📤 أرسل ملف التطبيق الآن وسيتم نشره تلقائياً في قناة قاعدة البيانات:",
                chat_id, message_id
            )

        # ========== VIP ==========
        elif data == "show_vip" or data.startswith("show_vip_p_"):
            user = get_user(tg_id)
            if not user or not user[3]:
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("🔓 احصل على VIP", callback_data="buy_vip"))
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
                bot.edit_message_text("👑 *VIP*\nللوصول تحتاج إحالات.", chat_id, message_id,
                                      parse_mode='Markdown', reply_markup=keyboard)
                return
            page = 1
            if "show_vip_p_" in data:
                page = int(data.split("_")[-1])
            apps, total = get_all_vip_apps(page)
            keyboard = InlineKeyboardMarkup(row_width=1)
            nav = []
            if page > 1:
                nav.append(InlineKeyboardButton("⏪", callback_data=f"show_vip_p_{page-1}"))
            if page * 5 < total:
                nav.append(InlineKeyboardButton("⏩", callback_data=f"show_vip_p_{page+1}"))
            if nav:
                keyboard.add(*nav)
            for app_code, name, downloads in apps:
                keyboard.add(InlineKeyboardButton(f"👑 {name} ({downloads})", callback_data=f"appv_{app_code}"))
            if is_admin(tg_id):
                keyboard.add(InlineKeyboardButton("➕ إضافة VIP", callback_data="add_vip_app"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text(f"👑 VIP — صفحة {page}", chat_id, message_id,
                                  parse_mode='Markdown', reply_markup=keyboard)

        elif data == "buy_vip":
            required = int(get_setting('referrals_for_vip') or 10)
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='completed'", (tg_id,))
            count = c.fetchone()[0]
            conn.close()
            if count >= required:
                set_vip(tg_id)
                bot.edit_message_text("🎉 أصبحت VIP!", chat_id, message_id)
                bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
            else:
                bot.answer_callback_query(call.id, f"تحتاج {required} إحالة، لديك {count}.", show_alert=True)

        elif data == "add_vip_app":
            if not is_admin(tg_id):
                return
            user_states[tg_id] = f"waiting_app_file|0|1"
            bot.edit_message_text(
                "📤 أرسل ملف تطبيق VIP الآن وسيتم نشره تلقائياً في قناة قاعدة البيانات:",
                chat_id, message_id
            )

        # ========== الرصيد والإحالات ==========
        elif data == "show_balance":
            user = get_user(tg_id)
            if not user:
                return
            balance = user[2]
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='completed'", (tg_id,))
            refs = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='pending'", (tg_id,))
            pending = c.fetchone()[0]
            conn.close()
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔗 رابط الدعوة", callback_data="get_referral_link"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text(
                f"💰 *الرصيد:* {balance} دولار\n"
                f"📊 *الإحالات الناجحة:* {refs}\n"
                f"⏳ *معلقة:* {pending}\n\n"
                f"كل إحالة = 0.5 دولار\n"
                f"• {get_setting('referrals_for_feature')} إحالات = طلب كسر/رفع\n"
                f"• {get_setting('referrals_for_vip')} إحالات = VIP",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        elif data == "get_referral_link":
            link = f"https://t.me/{bot.get_me().username}?start=ref_{tg_id}"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text(f"🔗 *رابط دعوتك:*\n`{link}`",
                                  chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)

        # ========== طلب كسر / رفع ==========
        elif data == "make_request":
            required = float(get_setting('referrals_for_feature') or 2) * 0.5
            user = get_user(tg_id)
            if not user or user[2] < required:
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("🔗 جلب إحالات", callback_data="get_referral_link"))
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
                bot.edit_message_text(f"❌ رصيدك غير كافٍ (تحتاج {required} دولار).",
                                      chat_id, message_id, reply_markup=keyboard)
                return
            keyboard = InlineKeyboardMarkup(row_width=2)
            keyboard.add(InlineKeyboardButton("🔨 طلب كسر", callback_data="request_crack"))
            keyboard.add(InlineKeyboardButton("📤 رفع تطبيق", callback_data="request_upload"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text("📨 اختر نوع الطلب:", chat_id, message_id, reply_markup=keyboard)

        elif data == "request_crack":
            user_states[tg_id] = "waiting_crack_request"
            bot.edit_message_text("✏️ أرسل اسم التطبيق:", chat_id, message_id)

        elif data == "request_upload":
            user_states[tg_id] = "waiting_upload_file"
            bot.edit_message_text("📤 أرسل ملف التطبيق:", chat_id, message_id)

        # ========== لوحة الإدمن ==========
        elif data == "admin_panel":
            if not is_admin(tg_id):
                return
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                InlineKeyboardButton("📊 الإحصائيات", callback_data="admin_stats"),
                InlineKeyboardButton("📥 الطلبات المعلقة", callback_data="view_requests"),
                InlineKeyboardButton("📂 إدارة التصنيفات", callback_data="show_categories"),
                InlineKeyboardButton("👑 إدارة VIP", callback_data="show_vip"),
                InlineKeyboardButton("⚙️ الإعدادات", callback_data="edit_settings"),
                InlineKeyboardButton("👥 المستخدمين", callback_data="admin_users"),
                InlineKeyboardButton("📋 سجل الإدمن", callback_data="admin_logs"),
                InlineKeyboardButton("🔧 وضع الصيانة", callback_data="toggle_maintenance"),
                InlineKeyboardButton("💾 نسخ احتياطي", callback_data="backup_db"),
                InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")
            )
            bot.edit_message_text("⚙️ *لوحة الإدارة*", chat_id, message_id,
                                  parse_mode='Markdown', reply_markup=keyboard)

        elif data == "admin_stats":
            if not is_admin(tg_id):
                return
            tu, ta, tv, pr, tr, tb = get_stats()
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel"))
            bot.edit_message_text(
                f"📊 *الإحصائيات*\n\n"
                f"👥 المستخدمين: {tu}\n"
                f"📱 تطبيقات عادية: {ta}\n"
                f"👑 تطبيقات VIP: {tv}\n"
                f"📨 طلبات معلقة: {pr}\n"
                f"🔗 إحالات ناجحة: {tr}\n"
                f"💰 إجمالي الرصيد: {tb:.1f} دولار",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        elif data == "admin_users":
            if not is_admin(tg_id):
                return
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT tg_id, username, balance, is_vip, join_date FROM users ORDER BY join_date DESC LIMIT 20")
            users = c.fetchall()
            conn.close()
            text = "👥 *آخر 20 مستخدم*\n\n"
            for uid, username, balance, vip, join_date in users:
                status = "⭐ VIP" if vip else "عادي"
                text += f"🆔 {uid} | @{username or 'بدون'}\n💰 {balance:.1f} | {status}\n📅 {join_date[:10]}\n\n"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel"))
            bot.edit_message_text(text, chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)

        elif data == "admin_logs":
            if not is_admin(tg_id):
                return
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT action, details, created_at FROM admin_logs ORDER BY id DESC LIMIT 20")
            logs = c.fetchall()
            conn.close()
            text = "📋 *آخر 20 سجل*\n\n"
            for action, details, created_at in logs:
                text += f"• {action}: {details}\n🕐 {created_at[:16]}\n\n"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel"))
            bot.edit_message_text(text or "لا توجد سجلات.", chat_id, message_id,
                                  parse_mode='Markdown', reply_markup=keyboard)

        elif data == "toggle_maintenance":
            if not is_admin(tg_id):
                return
            current = get_setting('maintenance_mode')
            new = '0' if current == '1' else '1'
            set_setting('maintenance_mode', new)
            state_text = 'مفعّل' if new == '1' else 'معطّل'
            log_admin_action(tg_id, "toggle_maintenance", f"وضع الصيانة: {state_text}")
            bot.answer_callback_query(call.id, f"✅ وضع الصيانة {state_text}.")
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                InlineKeyboardButton("📊 الإحصائيات", callback_data="admin_stats"),
                InlineKeyboardButton("📥 الطلبات المعلقة", callback_data="view_requests"),
                InlineKeyboardButton("📂 إدارة التصنيفات", callback_data="show_categories"),
                InlineKeyboardButton("👑 إدارة VIP", callback_data="show_vip"),
                InlineKeyboardButton("⚙️ الإعدادات", callback_data="edit_settings"),
                InlineKeyboardButton("👥 المستخدمين", callback_data="admin_users"),
                InlineKeyboardButton("📋 سجل الإدمن", callback_data="admin_logs"),
                InlineKeyboardButton("🔧 وضع الصيانة", callback_data="toggle_maintenance"),
                InlineKeyboardButton("💾 نسخ احتياطي", callback_data="backup_db"),
                InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")
            )
            bot.edit_message_text("⚙️ *لوحة الإدارة*", chat_id, message_id,
                                  parse_mode='Markdown', reply_markup=keyboard)

        elif data == "backup_db":
            if not is_admin(tg_id):
                return
            try:
                ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
                backup_path = os.path.join(BACKUP_DIR, f"manual_{ts}.db")
                src = sqlite3.connect(DB_PATH)
                dst = sqlite3.connect(backup_path)
                src.backup(dst)
                dst.close()
                src.close()
                with open(backup_path, 'rb') as f:
                    bot.send_document(tg_id, f, caption=f"💾 نسخة يدوية {ts}")
                log_admin_action(tg_id, "manual_backup", ts)
                bot.answer_callback_query(call.id, "✅ تم إنشاء النسخة وإرسالها.")
            except Exception as e:
                bot.answer_callback_query(call.id, f"❌ خطأ: {e}", show_alert=True)

        elif data == "edit_settings":
            if not is_admin(tg_id):
                return
            settings = {
                'الإحالات للطلب': 'referrals_for_feature',
                'الإحالات للـVIP': 'referrals_for_vip',
                'أيام صلاحية الإحالة': 'referral_expire_days',
                'ساعات المهلة': 'grace_period_hours'
            }
            keyboard = InlineKeyboardMarkup(row_width=1)
            for label, key in settings.items():
                val = get_setting(key)
                keyboard.add(InlineKeyboardButton(f"{label}: {val}", callback_data=f"editsetting|{key}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel"))
            bot.edit_message_text("⚙️ *الإعدادات*", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)

        elif data.startswith("editsetting|"):
            if not is_admin(tg_id):
                return
            key = data.split("|", 1)[1]
            user_states[tg_id] = f"edit_setting|{key}"
            bot.edit_message_text(f"✏️ أرسل القيمة الجديدة لـ {key}:", chat_id, message_id)

        # ========== الطلبات ==========
        elif data == "view_requests":
            if not is_admin(tg_id):
                return
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT id, user_id, type, app_name FROM user_requests WHERE status='pending'")
            reqs = c.fetchall()
            conn.close()
            if not reqs:
                bot.answer_callback_query(call.id, "لا توجد طلبات معلقة.")
                return
            keyboard = InlineKeyboardMarkup(row_width=1)
            for req_id, user_id, typ, name in reqs:
                label = f"{'🔨' if typ == 'crack' else '📤'} {name[:20]}"
                keyboard.add(InlineKeyboardButton(label, callback_data=f"review_req_{req_id}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel"))
            bot.edit_message_text("📋 *الطلبات المعلقة:*", chat_id, message_id,
                                  parse_mode='Markdown', reply_markup=keyboard)

        elif data.startswith("review_req_"):
            if not is_admin(tg_id):
                return
            req_id = int(data.split("_")[2])
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT user_id, type, app_name, description, file_id FROM user_requests WHERE id=?", (req_id,))
            req = c.fetchone()
            conn.close()
            if not req:
                bot.answer_callback_query(call.id, "الطلب غير موجود.")
                return
            keyboard = InlineKeyboardMarkup(row_width=2)
            keyboard.add(InlineKeyboardButton("✅ موافقة", callback_data=f"approve_req_{req_id}"))
            keyboard.add(InlineKeyboardButton("❌ رفض", callback_data=f"reject_req_{req_id}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="view_requests"))
            text = f"طلب #{req_id}\nالمستخدم: {req[0]}\nالنوع: {req[1]}\nالاسم: {req[2]}\nالوصف: {req[3] or 'لا يوجد'}"
            bot.edit_message_text(text, chat_id, message_id, reply_markup=keyboard)

        elif data.startswith("approve_req_"):
            if not is_admin(tg_id):
                return
            req_id = int(data.split("_")[2])
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT user_id, type, app_name FROM user_requests WHERE id=?", (req_id,))
            req = c.fetchone()
            if req:
                user_id, typ, name = req
                if typ == 'crack':
                    bot.send_message(user_id, f"✅ تم الموافقة على طلب كسر '{name}'.")
                else:
                    bot.send_message(user_id, f"✅ تم قبول رفع '{name}'. سيتم نشره قريباً.")
                c.execute("UPDATE user_requests SET status='approved', admin_feedback='تمت الموافقة' WHERE id=?", (req_id,))
                conn.commit()
                log_admin_action(tg_id, "approve_request", f"{req_id} - {name}")
            conn.close()
            bot.edit_message_text("✅ تمت الموافقة.", chat_id, message_id,
                                  reply_markup=InlineKeyboardMarkup().add(
                                      InlineKeyboardButton("🔙 رجوع", callback_data="view_requests")))

        elif data.startswith("reject_req_"):
            if not is_admin(tg_id):
                return
            req_id = int(data.split("_")[2])
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT user_id, app_name FROM user_requests WHERE id=?", (req_id,))
            row = c.fetchone()
            if row:
                user_id, name = row
                update_balance(user_id, 1.0)
                bot.send_message(user_id, f"❌ تم رفض طلب '{name}'، تم إعادة رصيدك.")
                c.execute("UPDATE user_requests SET status='rejected', admin_feedback='تم الرفض' WHERE id=?", (req_id,))
                conn.commit()
                log_admin_action(tg_id, "reject_request", f"{req_id} - {name}")
            conn.close()
            bot.edit_message_text("❌ تم الرفض وإعادة الرصيد.", chat_id, message_id,
                                  reply_markup=InlineKeyboardMarkup().add(
                                      InlineKeyboardButton("🔙 رجوع", callback_data="view_requests")))

        elif data == "dummy":
            bot.answer_callback_query(call.id)

        else:
            bot.answer_callback_query(call.id, "زر غير معروف.")

    except Exception as e:
        print(f"Callback error: {e}")
        try:
            bot.answer_callback_query(call.id, "حدث خطأ.", show_alert=True)
        except:
            pass

# ======================= معالجات الرسائل =======================
@bot.message_handler(commands=['start'])
def start_command(message):
    tg_id = message.from_user.id
    username = message.from_user.username or "NoUsername"

    referrer_id = None
    if ' ' in message.text:
        payload = message.text.split(' ', 1)[1]
        if payload.startswith('ref_'):
            try:
                referrer_id = int(payload.split('_')[1])
                if referrer_id == tg_id:
                    referrer_id = None
            except:
                pass

    is_new = register_user(tg_id, username, referrer_id)
    if is_new and referrer_id:
        add_referral(referrer_id, tg_id)
        bot.send_message(tg_id, "✅ تم التسجيل عن طريق دعوة!")

    threading.Thread(target=process_pending_referrals, daemon=True).start()

    if get_setting('maintenance_mode') == '1' and not is_admin(tg_id):
        bot.send_message(tg_id, "🔧 البوت في وضع الصيانة، عذراً.")
        return

    if not check_subscription(tg_id):
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("📢 اشترك في القناة", url=FORCE_SUB_CHANNEL_URL))
        keyboard.add(InlineKeyboardButton("🔄 تأكد", callback_data="check_subscription"))
        bot.send_message(tg_id, "⚠️ يجب الاشتراك في القناة:", reply_markup=keyboard)
        return

    bot.send_message(tg_id, "مرحباً بك 🚀", reply_markup=main_menu_keyboard(tg_id))

# ========== استقبال ملف التطبيق من الإدمن ونشره تلقائياً في قناة DB ==========
@bot.message_handler(content_types=['document', 'video', 'audio', 'photo'],
                     func=lambda m: is_admin(m.from_user.id)
                     and isinstance(user_states.get(m.from_user.id, ""), str)
                     and user_states.get(m.from_user.id, "").startswith("waiting_app_file|"))
def receive_app_file(message):
    tg_id = message.from_user.id
    state = user_states[tg_id]
    parts = state.split("|")
    cat_id = int(parts[1])   # 0 = VIP
    is_vip = int(parts[2])

    # استخراج معلومات الملف (الاسم يُؤخذ تلقائياً من اسم الملف)
    if message.document:
        file_name = message.document.file_name or "تطبيق"
    elif message.video:
        file_name = message.video.file_name or "فيديو"
    elif message.audio:
        file_name = message.audio.file_name or "صوت"
    else:
        file_name = "تطبيق"

    # ننتقل مباشرة لخطوة الوصف فقط
    user_states[tg_id] = f"waiting_app_caption|{cat_id}|{is_vip}|{message.message_id}|{file_name}"
    # نحفظ الرسالة الأصلية في الذاكرة لنعيد استخدامها
    pending_app_messages[tg_id] = message
    bot.reply_to(message, f"✅ استلمت الملف: *{file_name}*\n\n✏️ أرسل وصف التطبيق (أو أرسل /skip للتخطي):",
                 parse_mode='Markdown')

@bot.message_handler(func=lambda m: is_admin(m.from_user.id)
                     and isinstance(user_states.get(m.from_user.id, ""), str)
                     and user_states.get(m.from_user.id, "").startswith("waiting_app_caption|"))
def receive_app_caption_and_publish(message):
    tg_id = message.from_user.id
    state = user_states[tg_id]
    parts = state.split("|", 4)
    cat_id = int(parts[1])
    is_vip = int(parts[2])
    original_msg_id = int(parts[3])
    name = parts[4]

    description = "" if message.text.strip() in ["/skip", "تخطي"] else message.text.strip()
    cat_name = "VIP" if is_vip else (get_category_name(cat_id) or "عام")

    # ==== النشر التلقائي في قناة DB ====
    original_message = pending_app_messages.get(tg_id)
    if not original_message:
        bot.reply_to(message, "❌ فُقدت بيانات الملف. أعد المحاولة.")
        user_states.pop(tg_id, None)
        return

    caption_text = f"📱 *{name}*"
    if description:
        caption_text += f"\n\n{description}"
    if is_vip:
        caption_text = f"👑 VIP\n" + caption_text
    caption_text += f"\n\n📁 {cat_name}"

    try:
        # البوت ينشر الملف في قناة DB مع الوصف
        if original_message.document:
            sent = bot.send_document(
                DB_CHANNEL_ID,
                original_message.document.file_id,
                caption=caption_text,
                parse_mode='Markdown'
            )
        elif original_message.video:
            sent = bot.send_video(
                DB_CHANNEL_ID,
                original_message.video.file_id,
                caption=caption_text,
                parse_mode='Markdown'
            )
        elif original_message.audio:
            sent = bot.send_audio(
                DB_CHANNEL_ID,
                original_message.audio.file_id,
                caption=caption_text,
                parse_mode='Markdown'
            )
        else:
            bot.reply_to(message, "❌ نوع الملف غير مدعوم.")
            user_states.pop(tg_id, None)
            pending_app_messages.pop(tg_id, None)
            return

        # نأخذ message_id من رسالة القناة وننشئ الكود المشفر
        channel_msg_id = sent.message_id
        app_code = register_app_in_channel(channel_msg_id, name, cat_name, bool(is_vip))

        log_admin_action(tg_id, "add_app", f"code={app_code}, msg_id={channel_msg_id}, name={name}, vip={is_vip}, cat={cat_name}")
        user_states.pop(tg_id, None)
        pending_app_messages.pop(tg_id, None)

        bot.reply_to(message,
            f"✅ تم نشر التطبيق في قناة قاعدة البيانات!\n\n"
            f"📱 *{name}*\n"
            f"📁 التصنيف: {cat_name}\n"
            f"🔒 الكود الداخلي: `{app_code}`",
            parse_mode='Markdown'
        )
        bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))

    except Exception as e:
        bot.reply_to(message, f"❌ فشل النشر في قناة DB: {e}\n\nتأكد أن البوت مشرف في القناة.")
        user_states.pop(tg_id, None)
        pending_app_messages.pop(tg_id, None)

# ========== اسم التصنيف ==========
@bot.message_handler(func=lambda m: user_states.get(m.from_user.id) == "waiting_category_name")
def receive_category_name(message):
    if not is_admin(message.from_user.id):
        return
    name = message.text.strip()
    if not name:
        bot.reply_to(message, "الاسم غير صالح.")
        return
    try:
        conn = db_conn()
        c = conn.cursor()
        c.execute("INSERT INTO categories (name, admin_id, created_at) VALUES (?, ?, ?)",
                  (name, message.from_user.id, datetime.datetime.now().isoformat()))
        conn.commit()
        conn.close()
        log_admin_action(message.from_user.id, "add_category", f"تصنيف: {name}")
        bot.reply_to(message, f"✅ تم إضافة التصنيف: {name}")
        user_states.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(message.from_user.id))
    except sqlite3.IntegrityError:
        bot.reply_to(message, "❌ هذا التصنيف موجود مسبقاً.")

# ========== البحث ==========
@bot.message_handler(func=lambda m: user_states.get(m.from_user.id) == "waiting_search_query")
def receive_search_query(message):
    tg_id = message.from_user.id
    query = message.text.strip()
    if len(query) < 2:
        bot.reply_to(message, "❌ اكتب كلمة بحث أكثر من حرف.")
        return
    apps, total = search_apps(query)
    if not apps:
        bot.reply_to(message, "❌ لا توجد نتائج.")
        user_states.pop(tg_id, None)
        return
    keyboard = InlineKeyboardMarkup(row_width=1)
    for app_code, name, downloads in apps[:10]:
        keyboard.add(InlineKeyboardButton(f"📱 {name} ({downloads}⬇️)", callback_data=f"appv_{app_code}"))
    keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
    bot.reply_to(message, f"🔍 نتائج البحث عن '{query}' ({total}):", reply_markup=keyboard)
    user_states.pop(tg_id, None)

# ========== طلب كسر ==========
@bot.message_handler(func=lambda m: user_states.get(m.from_user.id) == "waiting_crack_request")
def receive_crack_request(message):
    tg_id = message.from_user.id
    required = float(get_setting('referrals_for_feature') or 2) * 0.5
    user = get_user(tg_id)
    if not user or user[2] < required:
        bot.reply_to(message, "❌ رصيدك غير كافٍ.")
        user_states.pop(tg_id, None)
        return
    update_balance(tg_id, -required)
    conn = db_conn()
    c = conn.cursor()
    c.execute("INSERT INTO user_requests (user_id, type, app_name, status, created_at) VALUES (?, ?, ?, ?, ?)",
              (tg_id, 'crack', message.text, 'pending', datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()
    bot.reply_to(message, "✅ تم إرسال طلب الكسر للإدمن.")
    user_states.pop(tg_id, None)
    bot.send_message(ADMIN_ID, f"📩 طلب كسر جديد من @{message.from_user.username or tg_id}\nالتطبيق: {message.text}")

# ========== رفع ملف (طلب مستخدم) ==========
@bot.message_handler(content_types=['document'],
                     func=lambda m: user_states.get(m.from_user.id) == "waiting_upload_file")
def handle_upload_request(message):
    tg_id = message.from_user.id
    required = float(get_setting('referrals_for_feature') or 2) * 0.5
    user = get_user(tg_id)
    if not user or user[2] < required:
        bot.reply_to(message, "❌ رصيدك غير كافٍ.")
        user_states.pop(tg_id, None)
        return
    update_balance(tg_id, -required)
    file_id = message.document.file_id
    file_name = message.document.file_name or "تطبيق"
    user_states[tg_id] = f"waiting_upload_desc|{file_id}|{file_name}"
    bot.reply_to(message, "✏️ أرسل وصف التطبيق:")

@bot.message_handler(func=lambda m: isinstance(user_states.get(m.from_user.id, ""), str)
                     and user_states.get(m.from_user.id, "").startswith("waiting_upload_desc|"))
def receive_upload_desc(message):
    tg_id = message.from_user.id
    state = user_states[tg_id]
    parts = state.split("|", 2)
    file_id = parts[1]
    file_name = parts[2]
    description = message.text
    conn = db_conn()
    c = conn.cursor()
    c.execute("INSERT INTO user_requests (user_id, type, app_name, description, file_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (tg_id, 'upload', file_name, description, file_id, 'pending', datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()
    bot.reply_to(message, "✅ تم إرسال طلب الرفع للإدمن.")
    user_states.pop(tg_id, None)
    bot.send_message(ADMIN_ID, f"📩 طلب رفع جديد من @{message.from_user.username or tg_id}\nالتطبيق: {file_name}")

# ========== تعديل إعداد ==========
@bot.message_handler(func=lambda m: isinstance(user_states.get(m.from_user.id, ""), str)
                     and user_states.get(m.from_user.id, "").startswith("edit_setting|"))
def edit_setting_value(message):
    tg_id = message.from_user.id
    if not is_admin(tg_id):
        return
    key = user_states[tg_id].split("|", 1)[1]
    value = message.text.strip()
    try:
        float(value)
        set_setting(key, value)
        log_admin_action(tg_id, "edit_setting", f"{key} = {value}")
        bot.reply_to(message, f"✅ تم تحديث {key} إلى {value}")
        user_states.pop(tg_id, None)
        bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
    except ValueError:
        bot.reply_to(message, "❌ القيمة يجب أن تكون رقماً.")

# ======================= تشغيل البوت =======================
if __name__ == "__main__":
    bot.set_my_commands([telebot.types.BotCommand("start", "بدء البوت")])
    threading.Thread(target=process_pending_referrals, daemon=True).start()
    print("✅ البوت يعمل — نظام قناة DB مفعّل...")

    while True:
        try:
            bot.polling(non_stop=True, timeout=20, long_polling_timeout=15, skip_pending=True)
        except Exception as e:
            print(f"⚠️ خطأ: {e}. إعادة في 10 ثوان...")
            time.sleep(10)