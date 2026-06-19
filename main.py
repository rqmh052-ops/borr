import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
import datetime
import time
import os

# ======================= الثوابت =======================
BOT_TOKEN = "8737177889:AAGnnxZq9Yyptc1cdLTpEv5FoNbT5Jn8SQY"
ADMIN_ID = 8287678319
CHANNEL_ID = -1004325834135
CHANNEL_URL = "https://t.me/Bayan_x777"
# =====================================================

bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}

# ======================= قاعدة البيانات =======================
def init_db():
    conn = sqlite3.connect('bot_data.db', check_same_thread=False)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0,
        is_vip INTEGER DEFAULT 0, vip_activated_date TEXT, referrer_id INTEGER,
        join_date TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER, referred_id INTEGER,
        status TEXT, created_at TEXT, completed_at TEXT, channel_checked_date TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS banned_pairs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user1_id INTEGER, user2_id INTEGER
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, admin_id INTEGER,
        created_at TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS apps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER, name TEXT,
        description TEXT, file_id TEXT, admin_id INTEGER, added_date TEXT,
        download_count INTEGER DEFAULT 0
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS vip_apps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, description TEXT,
        file_id TEXT, admin_id INTEGER, added_date TEXT,
        download_count INTEGER DEFAULT 0
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, type TEXT,
        app_name TEXT, description TEXT, file_id TEXT, status TEXT,
        admin_feedback TEXT, created_at TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS admin_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, action TEXT,
        details TEXT, created_at TEXT
    )''')
    
    default_settings = {
        'referrals_for_feature': '2',
        'referrals_for_vip': '10',
        'referral_expire_days': '4',
        'grace_period_hours': '24',
        'maintenance_mode': '0'
    }
    for key, val in default_settings.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))
    
    conn.commit()
    conn.close()

init_db()

def db_conn():
    return sqlite3.connect('bot_data.db', check_same_thread=False)

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
        c.execute("INSERT INTO users (tg_id, username, referrer_id, join_date) VALUES (?, ?, ?, ?)",
                  (tg_id, username, referrer_id, datetime.datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return True
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
    c.execute("SELECT * FROM referrals WHERE referrer_id=? AND referred_id=?", (referrer_id, referred_id))
    if c.fetchone():
        conn.close()
        return False
    c.execute("INSERT INTO referrals (referrer_id, referred_id, status, created_at) VALUES (?, ?, ?, ?)",
              (referrer_id, referred_id, 'pending', datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return True

def check_subscription(tg_id):
    try:
        member = bot.get_chat_member(CHANNEL_ID, tg_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

def process_pending_referrals():
    conn = db_conn()
    c = conn.cursor()
    now = datetime.datetime.now()
    expire_days = int(get_setting('referral_expire_days'))
    grace_hours = int(get_setting('grace_period_hours'))
    
    c.execute("SELECT id, referrer_id, referred_id, created_at FROM referrals WHERE status='pending'")
    pendings = c.fetchall()
    for ref_id, referrer_id, referred_id, created_at in pendings:
        created_time = datetime.datetime.fromisoformat(created_at)
        days_passed = (now - created_time).days
        
        if not check_subscription(referred_id):
            last_check = c.execute("SELECT channel_checked_date FROM referrals WHERE id=?", (ref_id,)).fetchone()
            if last_check and last_check[0]:
                last_time = datetime.datetime.fromisoformat(last_check[0])
                if (now - last_time).seconds > grace_hours * 3600:
                    c.execute("UPDATE referrals SET status='cancelled' WHERE id=?", (ref_id,))
                    conn.commit()
                    continue
            else:
                c.execute("UPDATE referrals SET channel_checked_date=? WHERE id=?", (now.isoformat(), ref_id))
                conn.commit()
                continue
        else:
            if days_passed >= expire_days:
                c.execute("UPDATE referrals SET status='completed', completed_at=? WHERE id=?", (now.isoformat(), ref_id))
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

def get_apps_by_category(category_id, page=1, per_page=5):
    conn = db_conn()
    c = conn.cursor()
    offset = (page - 1) * per_page
    c.execute("SELECT id, name, description, file_id, download_count FROM apps WHERE category_id=? LIMIT ? OFFSET ?",
              (category_id, per_page, offset))
    apps = c.fetchall()
    c.execute("SELECT COUNT(*) FROM apps WHERE category_id=?", (category_id,))
    total = c.fetchone()[0]
    conn.close()
    return apps, total

def get_vip_apps(page=1, per_page=5):
    conn = db_conn()
    c = conn.cursor()
    offset = (page - 1) * per_page
    c.execute("SELECT id, name, description, file_id, download_count FROM vip_apps LIMIT ? OFFSET ?", (per_page, offset))
    apps = c.fetchall()
    c.execute("SELECT COUNT(*) FROM vip_apps")
    total = c.fetchone()[0]
    conn.close()
    return apps, total

def get_total_users():
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total = c.fetchone()[0]
    conn.close()
    return total

def get_total_apps():
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM apps")
    total = c.fetchone()[0]
    conn.close()
    return total

def get_total_vip_apps():
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM vip_apps")
    total = c.fetchone()[0]
    conn.close()
    return total

def get_pending_requests():
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM user_requests WHERE status='pending'")
    total = c.fetchone()[0]
    conn.close()
    return total

def search_apps(query, page=1, per_page=10):
    conn = db_conn()
    c = conn.cursor()
    offset = (page - 1) * per_page
    c.execute("SELECT id, name, description, file_id, category_id FROM apps WHERE name LIKE ? OR description LIKE ? LIMIT ? OFFSET ?",
              (f"%{query}%", f"%{query}%", per_page, offset))
    apps = c.fetchall()
    c.execute("SELECT COUNT(*) FROM apps WHERE name LIKE ? OR description LIKE ?", (f"%{query}%", f"%{query}%"))
    total = c.fetchone()[0]
    conn.close()
    return apps, total

# ======================= واجهات الأزرار =======================
def main_menu_keyboard(tg_id):
    keyboard = InlineKeyboardMarkup(row_width=2)
    buttons = [
        InlineKeyboardButton("📂 التطبيقات", callback_data="show_categories"),
        InlineKeyboardButton("👑 VIP", callback_data="show_vip"),
        InlineKeyboardButton("💰 رصيدي", callback_data="show_balance"),
        InlineKeyboardButton("🔗 رابط الدعوة", callback_data="get_referral_link"),
        InlineKeyboardButton("📨 طلب كسر / رفع", callback_data="make_request"),
        InlineKeyboardButton("🔍 بحث", callback_data="search_apps")
    ]
    keyboard.add(*buttons[:2])
    keyboard.add(*buttons[2:4])
    keyboard.add(buttons[4])
    keyboard.add(buttons[5])
    if is_admin(tg_id):
        keyboard.add(InlineKeyboardButton("⚙️ لوحة الإدارة", callback_data="admin_panel"))
    return keyboard

# ======================= المعالج المركزي للأزرار =======================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    try:
        data = call.data
        tg_id = call.from_user.id
        message_id = call.message.message_id
        chat_id = call.message.chat.id
        
        # التحقق من وضع الصيانة
        if get_setting('maintenance_mode') == '1' and not is_admin(tg_id) and data not in ['check_subscription', 'dummy']:
            bot.answer_callback_query(call.id, "🔧 البوت في وضع الصيانة حالياً، عذراً.", show_alert=True)
            return
        
        if data not in ['check_subscription', 'dummy']:
            if not check_subscription(tg_id):
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("📢 اشترك في القناة", url=CHANNEL_URL))
                keyboard.add(InlineKeyboardButton("🔄 تأكد", callback_data="check_subscription"))
                bot.edit_message_text("⚠️ يجب الاشتراك في القناة:", chat_id, message_id, reply_markup=keyboard)
                return
        
        # ========== القائمة الرئيسية ==========
        if data == "main_menu":
            bot.edit_message_text("اختر من القائمة:", chat_id, message_id, reply_markup=main_menu_keyboard(tg_id))
        
        # ========== الاشتراك ==========
        elif data == "check_subscription":
            if check_subscription(tg_id):
                bot.edit_message_text("✅ تم التأكيد!", chat_id, message_id)
                bot.send_message(tg_id, "اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
            else:
                bot.answer_callback_query(call.id, "❌ لم تشترك بعد.", show_alert=True)
        
        # ========== البحث ==========
        elif data == "search_apps":
            user_states[tg_id] = "waiting_search_query"
            bot.edit_message_text("🔍 اكتب كلمة البحث:", chat_id, message_id)
        
        # ========== التصنيفات ==========
        elif data == "show_categories":
            categories = get_categories()
            keyboard = InlineKeyboardMarkup(row_width=1)
            for cat_id, name in categories:
                keyboard.add(InlineKeyboardButton(f"📁 {name}", callback_data=f"cat_{cat_id}"))
            if is_admin(tg_id):
                keyboard.add(InlineKeyboardButton("➕ إضافة تصنيف", callback_data="add_category"))
                keyboard.add(InlineKeyboardButton("🗑️ حذف تصنيف", callback_data="delete_category_menu"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text("📂 اختر التصنيف:", chat_id, message_id, reply_markup=keyboard)
        
        elif data == "add_category":
            if not is_admin(tg_id):
                bot.answer_callback_query(call.id, "غير مصرح.")
                return
            user_states[tg_id] = "waiting_category_name"
            bot.edit_message_text("✏️ أرسل اسم التصنيف الجديد:", chat_id, message_id)
        
        elif data == "delete_category_menu":
            if not is_admin(tg_id):
                return
            categories = get_categories()
            if not categories:
                bot.answer_callback_query(call.id, "لا توجد تصنيفات لحذفها.")
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
            c.execute("DELETE FROM apps WHERE category_id=?", (cat_id,))
            c.execute("DELETE FROM categories WHERE id=?", (cat_id,))
            conn.commit()
            conn.close()
            log_admin_action(tg_id, "delete_category", f"حذف تصنيف: {cat_name}")
            bot.answer_callback_query(call.id, f"✅ تم حذف التصنيف {cat_name} وجميع تطبيقاته.")
            categories = get_categories()
            keyboard = InlineKeyboardMarkup(row_width=1)
            for cid, name in categories:
                keyboard.add(InlineKeyboardButton(f"📁 {name}", callback_data=f"cat_{cid}"))
            if is_admin(tg_id):
                keyboard.add(InlineKeyboardButton("➕ إضافة تصنيف", callback_data="add_category"))
                keyboard.add(InlineKeyboardButton("🗑️ حذف تصنيف", callback_data="delete_category_menu"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text("📂 اختر التصنيف:", chat_id, message_id, reply_markup=keyboard)
        
        # ========== عرض تطبيقات التصنيف ==========
        elif data.startswith("cat_"):
            parts = data.split("_")
            cat_id = int(parts[1])
            page = 1
            if len(parts) > 3 and parts[2] == "page":
                page = int(parts[3])
                cat_id = int(parts[1])
            apps, total = get_apps_by_category(cat_id, page)
            keyboard = InlineKeyboardMarkup(row_width=2)
            nav_buttons = []
            if page > 1:
                nav_buttons.append(InlineKeyboardButton("⏪", callback_data=f"cat_{cat_id}_page_{page-1}"))
            if page * 5 < total:
                nav_buttons.append(InlineKeyboardButton("⏩", callback_data=f"cat_{cat_id}_page_{page+1}"))
            if nav_buttons:
                keyboard.add(*nav_buttons)
            for app_id, name, desc, file_id, downloads in apps:
                keyboard.add(InlineKeyboardButton(f"📱 {name} ({downloads}⬇️)", callback_data=f"app_{app_id}"))
            if not apps:
                keyboard.add(InlineKeyboardButton("📭 لا توجد تطبيقات", callback_data="dummy"))
            if is_admin(tg_id):
                keyboard.add(InlineKeyboardButton("➕ إضافة تطبيق", callback_data=f"add_app_to_{cat_id}"))
                keyboard.add(InlineKeyboardButton("🗑️ حذف تطبيق", callback_data=f"delete_app_menu_{cat_id}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_categories"))
            cat_name = get_category_name(cat_id) or "تصنيف"
            bot.edit_message_text(f"📂 {cat_name} - صفحة {page} ({total} تطبيق)", chat_id, message_id, reply_markup=keyboard)
        
        elif data.startswith("add_app_to_"):
            if not is_admin(tg_id):
                bot.answer_callback_query(call.id, "غير مصرح.")
                return
            cat_id = int(data.split("_")[3])
            user_states[tg_id] = f"waiting_app_file_{cat_id}"
            bot.edit_message_text("📤 أرسل ملف APK الآن:", chat_id, message_id)
        
        elif data.startswith("delete_app_menu_"):
            if not is_admin(tg_id):
                return
            cat_id = int(data.split("_")[3])
            apps, total = get_apps_by_category(cat_id, 1, 20)
            if not apps:
                bot.answer_callback_query(call.id, "لا توجد تطبيقات في هذا التصنيف.")
                return
            keyboard = InlineKeyboardMarkup(row_width=1)
            for app_id, name, desc, file_id, downloads in apps:
                keyboard.add(InlineKeyboardButton(f"🗑️ {name}", callback_data=f"delete_app_{app_id}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data=f"cat_{cat_id}"))
            bot.edit_message_text("اختر تطبيقاً لحذفه:", chat_id, message_id, reply_markup=keyboard)
        
        elif data.startswith("delete_app_"):
            if not is_admin(tg_id):
                return
            app_id = int(data.split("_")[2])
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT name, category_id FROM apps WHERE id=?", (app_id,))
            app = c.fetchone()
            if app:
                app_name, cat_id = app
                c.execute("DELETE FROM apps WHERE id=?", (app_id,))
                conn.commit()
                log_admin_action(tg_id, "delete_app", f"حذف تطبيق: {app_name}")
                bot.answer_callback_query(call.id, f"✅ تم حذف {app_name}")
            conn.close()
            # العودة إلى قائمة التطبيقات
            apps, total = get_apps_by_category(cat_id)
            keyboard = InlineKeyboardMarkup(row_width=2)
            for aid, name, desc, file_id, downloads in apps:
                keyboard.add(InlineKeyboardButton(f"📱 {name}", callback_data=f"app_{aid}"))
            if not apps:
                keyboard.add(InlineKeyboardButton("📭 لا توجد تطبيقات", callback_data="dummy"))
            if is_admin(tg_id):
                keyboard.add(InlineKeyboardButton("➕ إضافة تطبيق", callback_data=f"add_app_to_{cat_id}"))
                keyboard.add(InlineKeyboardButton("🗑️ حذف تطبيق", callback_data=f"delete_app_menu_{cat_id}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_categories"))
            cat_name = get_category_name(cat_id) or "تصنيف"
            bot.edit_message_text(f"📂 {cat_name}", chat_id, message_id, reply_markup=keyboard)
        
        # ========== عرض التطبيق ==========
        elif data.startswith("app_"):
            app_id = int(data.split("_")[1])
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT name, description, file_id, download_count, category_id FROM apps WHERE id=?", (app_id,))
            app = c.fetchone()
            if app:
                name, desc, file_id, downloads, cat_id = app
                # تحديث عدد التحميلات
                c.execute("UPDATE apps SET download_count = download_count + 1 WHERE id=?", (app_id,))
                conn.commit()
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("📥 تحميل", callback_data=f"download_app_{file_id}_{name}"))
                if is_admin(tg_id):
                    keyboard.add(InlineKeyboardButton("✏️ تعديل", callback_data=f"edit_app_{app_id}"))
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data=f"cat_{cat_id}"))
                bot.edit_message_text(f"📱 *{name}*\n⬇️ {downloads+1} تحميل\n\n{desc}", 
                                      chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
            else:
                bot.answer_callback_query(call.id, "التطبيق غير موجود")
            conn.close()
        
        elif data.startswith("download_app_"):
            parts = data.split("_")
            file_id = parts[2]
            name = "_".join(parts[3:])
            try:
                bot.send_document(tg_id, file_id, caption=f"📱 {name}")
                bot.answer_callback_query(call.id, "✅ تم الإرسال")
            except Exception as e:
                bot.answer_callback_query(call.id, "❌ خطأ", show_alert=True)
                bot.send_message(ADMIN_ID, f"خطأ: {e}")
        
        elif data.startswith("edit_app_"):
            if not is_admin(tg_id):
                return
            app_id = int(data.split("_")[2])
            user_states[tg_id] = f"edit_app_name_{app_id}"
            bot.edit_message_text("✏️ أرسل الاسم الجديد للتطبيق:", chat_id, message_id)
        
        # ========== VIP ==========
        elif data == "show_vip":
            user = get_user(tg_id)
            if not user or not user[3]:
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("🔓 شراء VIP", callback_data="buy_vip"))
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
                bot.edit_message_text("👑 *VIP*\nللحصول على VIP تحتاج إحالات.", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
                return
            page = 1
            if "_page_" in data:
                page = int(data.split("_")[3])
            apps, total = get_vip_apps(page)
            keyboard = InlineKeyboardMarkup(row_width=2)
            nav = []
            if page > 1:
                nav.append(InlineKeyboardButton("⏪", callback_data=f"show_vip_page_{page-1}"))
            if page * 5 < total:
                nav.append(InlineKeyboardButton("⏩", callback_data=f"show_vip_page_{page+1}"))
            if nav:
                keyboard.add(*nav)
            for app_id, name, desc, file_id, downloads in apps:
                keyboard.add(InlineKeyboardButton(f"👑 {name} ({downloads})", callback_data=f"vip_app_{app_id}"))
            if is_admin(tg_id):
                keyboard.add(InlineKeyboardButton("➕ إضافة VIP", callback_data="add_vip_app"))
                keyboard.add(InlineKeyboardButton("🗑️ حذف VIP", callback_data="delete_vip_menu"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text(f"👑 VIP - صفحة {page}", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "buy_vip":
            required = int(get_setting('referrals_for_vip'))
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='completed'", (tg_id,))
            count = c.fetchone()[0]
            conn.close()
            if count >= required:
                set_vip(tg_id)
                bot.edit_message_text("🎉 أصبحت VIP!", chat_id, message_id)
                bot.send_message(tg_id, "اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
            else:
                bot.answer_callback_query(call.id, f"تحتاج {required} إحالة، لديك {count}.", show_alert=True)
        
        elif data.startswith("vip_app_"):
            app_id = int(data.split("_")[2])
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT name, description, file_id, download_count FROM vip_apps WHERE id=?", (app_id,))
            app = c.fetchone()
            if app:
                name, desc, file_id, downloads = app
                c.execute("UPDATE vip_apps SET download_count = download_count + 1 WHERE id=?", (app_id,))
                conn.commit()
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("📥 تحميل VIP", callback_data=f"download_vip_{file_id}_{name}"))
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_vip"))
                bot.edit_message_text(f"👑 *{name}*\n⬇️ {downloads+1}\n\n{desc}", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
            conn.close()
        
        elif data.startswith("download_vip_"):
            parts = data.split("_")
            file_id = parts[2]
            name = "_".join(parts[3:])
            try:
                bot.send_document(tg_id, file_id, caption=f"👑 VIP: {name}")
                bot.answer_callback_query(call.id, "✅ تم الإرسال")
            except Exception as e:
                bot.answer_callback_query(call.id, "❌ خطأ", show_alert=True)
                bot.send_message(ADMIN_ID, f"خطأ VIP: {e}")
        
        elif data == "add_vip_app":
            if not is_admin(tg_id):
                return
            user_states[tg_id] = "waiting_vip_file"
            bot.edit_message_text("📤 أرسل ملف VIP:", chat_id, message_id)
        
        elif data == "delete_vip_menu":
            if not is_admin(tg_id):
                return
            apps, total = get_vip_apps(1, 20)
            if not apps:
                bot.answer_callback_query(call.id, "لا توجد تطبيقات VIP.")
                return
            keyboard = InlineKeyboardMarkup(row_width=1)
            for app_id, name, desc, file_id, downloads in apps:
                keyboard.add(InlineKeyboardButton(f"🗑️ {name}", callback_data=f"delete_vip_{app_id}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_vip"))
            bot.edit_message_text("اختر تطبيق VIP لحذفه:", chat_id, message_id, reply_markup=keyboard)
        
        elif data.startswith("delete_vip_"):
            if not is_admin(tg_id):
                return
            app_id = int(data.split("_")[2])
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT name FROM vip_apps WHERE id=?", (app_id,))
            app = c.fetchone()
            if app:
                app_name = app[0]
                c.execute("DELETE FROM vip_apps WHERE id=?", (app_id,))
                conn.commit()
                log_admin_action(tg_id, "delete_vip_app", f"حذف VIP: {app_name}")
                bot.answer_callback_query(call.id, f"✅ تم حذف {app_name}")
            conn.close()
            apps, total = get_vip_apps(1, 20)
            keyboard = InlineKeyboardMarkup(row_width=1)
            for aid, name, desc, file_id, downloads in apps:
                keyboard.add(InlineKeyboardButton(f"👑 {name}", callback_data=f"vip_app_{aid}"))
            if not apps:
                keyboard.add(InlineKeyboardButton("📭 لا توجد تطبيقات VIP", callback_data="dummy"))
            if is_admin(tg_id):
                keyboard.add(InlineKeyboardButton("➕ إضافة VIP", callback_data="add_vip_app"))
                keyboard.add(InlineKeyboardButton("🗑️ حذف VIP", callback_data="delete_vip_menu"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text("👑 VIP", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
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
                f"كل إحالة = 0.5 دولار.\n"
                f"- {get_setting('referrals_for_feature')} إحالات = طلب كسر أو رفع.\n"
                f"- {get_setting('referrals_for_vip')} إحالات = VIP.",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "get_referral_link":
            link = f"https://t.me/{bot.get_me().username}?start=ref_{tg_id}"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text(f"🔗 *رابط دعوتك:*\n`{link}`", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
        # ========== طلب كسر / رفع ==========
        elif data == "make_request":
            required = int(get_setting('referrals_for_feature')) * 0.5
            user = get_user(tg_id)
            if not user or user[2] < required:
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("🔗 جلب إحالات", callback_data="get_referral_link"))
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
                bot.edit_message_text(f"❌ رصيدك غير كافٍ (تحتاج {required} دولار).", chat_id, message_id, reply_markup=keyboard)
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
                bot.answer_callback_query(call.id, "غير مصرح.")
                return
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                InlineKeyboardButton("📊 الإحصائيات", callback_data="admin_stats"),
                InlineKeyboardButton("📥 الطلبات المعلقة", callback_data="view_requests"),
                InlineKeyboardButton("📂 إدارة التصنيفات", callback_data="show_categories"),
                InlineKeyboardButton("👑 إدارة VIP", callback_data="show_vip"),
                InlineKeyboardButton("📊 الإعدادات", callback_data="edit_settings"),
                InlineKeyboardButton("👥 المستخدمين", callback_data="admin_users"),
                InlineKeyboardButton("📋 سجل الإدمن", callback_data="admin_logs"),
                InlineKeyboardButton("🔧 وضع الصيانة", callback_data="toggle_maintenance"),
                InlineKeyboardButton("💾 نسخ احتياطي", callback_data="backup_db"),
                InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")
            )
            bot.edit_message_text("⚙️ *لوحة الإدارة*", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "admin_stats":
            if not is_admin(tg_id):
                return
            total_users = get_total_users()
            total_apps = get_total_apps()
            total_vip = get_total_vip_apps()
            pending_reqs = get_pending_requests()
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM referrals WHERE status='completed'")
            total_refs = c.fetchone()[0]
            c.execute("SELECT SUM(balance) FROM users")
            total_balance = c.fetchone()[0] or 0
            conn.close()
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel"))
            bot.edit_message_text(
                f"📊 *الإحصائيات*\n\n"
                f"👥 المستخدمين: {total_users}\n"
                f"📱 التطبيقات: {total_apps}\n"
                f"👑 تطبيقات VIP: {total_vip}\n"
                f"📨 الطلبات المعلقة: {pending_reqs}\n"
                f"🔗 الإحالات الناجحة: {total_refs}\n"
                f"💰 إجمالي الرصيد: {total_balance} دولار",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "admin_users":
            if not is_admin(tg_id):
                return
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT tg_id, username, balance, is_vip, join_date FROM users ORDER BY join_date DESC LIMIT 20")
            users = c.fetchall()
            conn.close()
            if not users:
                bot.answer_callback_query(call.id, "لا يوجد مستخدمين.")
                return
            text = "👥 *آخر 20 مستخدم*\n\n"
            for uid, username, balance, vip, join_date in users:
                status = "⭐ VIP" if vip else "عادي"
                text += f"🆔 {uid} | @{username or 'بدون'}\n💰 {balance} | {status}\n📅 {join_date[:10]}\n\n"
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
            if not logs:
                bot.answer_callback_query(call.id, "لا توجد سجلات.")
                return
            text = "📋 *آخر 20 سجل*\n\n"
            for action, details, created_at in logs:
                text += f"• {action}: {details}\n🕐 {created_at[:16]}\n\n"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel"))
            bot.edit_message_text(text, chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "toggle_maintenance":
            if not is_admin(tg_id):
                return
            current = get_setting('maintenance_mode')
            new = '0' if current == '1' else '1'
            set_setting('maintenance_mode', new)
            log_admin_action(tg_id, "toggle_maintenance", f"وضع الصيانة: {'مفعل' if new == '1' else 'معطل'}")
            bot.answer_callback_query(call.id, f"✅ وضع الصيانة {'مفعل' if new == '1' else 'معطل'}.")
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                InlineKeyboardButton("📊 الإحصائيات", callback_data="admin_stats"),
                InlineKeyboardButton("📥 الطلبات المعلقة", callback_data="view_requests"),
                InlineKeyboardButton("📂 إدارة التصنيفات", callback_data="show_categories"),
                InlineKeyboardButton("👑 إدارة VIP", callback_data="show_vip"),
                InlineKeyboardButton("📊 الإعدادات", callback_data="edit_settings"),
                InlineKeyboardButton("👥 المستخدمين", callback_data="admin_users"),
                InlineKeyboardButton("📋 سجل الإدمن", callback_data="admin_logs"),
                InlineKeyboardButton("🔧 وضع الصيانة", callback_data="toggle_maintenance"),
                InlineKeyboardButton("💾 نسخ احتياطي", callback_data="backup_db"),
                InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")
            )
            bot.edit_message_text("⚙️ *لوحة الإدارة*", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "backup_db":
            if not is_admin(tg_id):
                return
            try:
                # إنشاء نسخة احتياطية
                backup_name = f"backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
                import shutil
                shutil.copy2('bot_data.db', backup_name)
                bot.send_document(tg_id, open(backup_name, 'rb'), caption=f"💾 نسخة احتياطية {backup_name}")
                os.remove(backup_name)
                log_admin_action(tg_id, "backup_db", "تم إنشاء نسخة احتياطية")
                bot.answer_callback_query(call.id, "✅ تم إنشاء النسخة الاحتياطية وإرسالها.")
            except Exception as e:
                bot.answer_callback_query(call.id, f"❌ خطأ: {e}", show_alert=True)
        
        # ========== الإعدادات ==========
        elif data == "edit_settings":
            if not is_admin(tg_id):
                return
            settings = {
                'الإحالات للطلب': 'referrals_for_feature',
                'الإحالات للVIP': 'referrals_for_vip',
                'أيام صلاحية الإحالة': 'referral_expire_days',
                'ساعات المهلة': 'grace_period_hours'
            }
            keyboard = InlineKeyboardMarkup(row_width=1)
            for label, key in settings.items():
                val = get_setting(key)
                keyboard.add(InlineKeyboardButton(f"{label}: {val}", callback_data=f"edit_setting_{key}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel"))
            bot.edit_message_text("📊 *الإعدادات*", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data.startswith("edit_setting_"):
            if not is_admin(tg_id):
                return
            key = data.split("_")[2]
            user_states[tg_id] = f"edit_setting_{key}"
            bot.edit_message_text(f"✏️ أرسل القيمة الجديدة لـ {key}:", chat_id, message_id)
        
        # ========== طلبات الموافقة ==========
        elif data == "view_requests":
            if not is_admin(tg_id):
                return
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT id, user_id, type, app_name FROM user_requests WHERE status='pending'")
            reqs = c.fetchall()
            conn.close()
            if not reqs:
                bot.answer_callback_query(call.id, "لا توجد طلبات.")
                return
            keyboard = InlineKeyboardMarkup(row_width=2)
            for req_id, user_id, typ, name in reqs:
                label = f"{'🔨' if typ == 'crack' else '📤'} {name[:15]}"
                keyboard.add(InlineKeyboardButton(label, callback_data=f"review_req_{req_id}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel"))
            bot.edit_message_text("📋 *الطلبات المعلقة:*", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
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
            if req[4]:
                text += f"\nFile ID: {req[4]}"
            bot.edit_message_text(text, chat_id, message_id, reply_markup=keyboard)
        
        elif data.startswith("approve_req_"):
            if not is_admin(tg_id):
                return
            req_id = int(data.split("_")[2])
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT user_id, type, app_name, description, file_id FROM user_requests WHERE id=?", (req_id,))
            req = c.fetchone()
            if not req:
                conn.close()
                return
            user_id, typ, name, desc, file_id = req
            if typ == 'crack':
                bot.send_message(user_id, f"✅ تم الموافقة على كسر '{name}'.")
            else:
                try:
                    c.execute("SELECT id FROM categories LIMIT 1")
                    cat = c.fetchone()
                    if cat:
                        c.execute("INSERT INTO apps (category_id, name, description, file_id, admin_id, added_date) VALUES (?, ?, ?, ?, ?, ?)",
                                  (cat[0], name, desc, file_id, ADMIN_ID, datetime.datetime.now().isoformat()))
                        bot.send_message(user_id, f"✅ تم نشر '{name}'!")
                except Exception as e:
                    bot.send_message(ADMIN_ID, f"خطأ: {e}")
            c.execute("UPDATE user_requests SET status='approved', admin_feedback='تمت الموافقة' WHERE id=?", (req_id,))
            conn.commit()
            conn.close()
            log_admin_action(tg_id, "approve_request", f"الطلب {req_id} - {name}")
            bot.edit_message_text("✅ تمت الموافقة.", chat_id, message_id)
            bot.send_message(tg_id, "اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
        
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
                bot.send_message(user_id, f"❌ تم رفض طلب '{name}'، تم إعادة الرصيد.")
            c.execute("UPDATE user_requests SET status='rejected', admin_feedback='تم الرفض' WHERE id=?", (req_id,))
            conn.commit()
            conn.close()
            log_admin_action(tg_id, "reject_request", f"الطلب {req_id}")
            bot.edit_message_text("❌ تم الرفض.", chat_id, message_id)
            bot.send_message(tg_id, "اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
        
        elif data == "dummy":
            bot.answer_callback_query(call.id)
        
        else:
            bot.answer_callback_query(call.id, "زر غير معروف")
    
    except Exception as e:
        print(f"Error: {e}")
        bot.answer_callback_query(call.id, "حدث خطأ.", show_alert=True)

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
        bot.send_message(tg_id, f"✅ تم التسجيل عن طريق دعوة!")
    
    process_pending_referrals()
    
    if get_setting('maintenance_mode') == '1' and not is_admin(tg_id):
        bot.send_message(tg_id, "🔧 البوت في وضع الصيانة حالياً، عذراً.")
        return
    
    if not check_subscription(tg_id):
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("📢 اشترك في القناة", url=CHANNEL_URL))
        keyboard.add(InlineKeyboardButton("🔄 تأكد", callback_data="check_subscription"))
        bot.send_message(tg_id, "⚠️ يجب الاشتراك في القناة:", reply_markup=keyboard)
        return
    
    bot.send_message(tg_id, "مرحباً بك في البوت 🚀", reply_markup=main_menu_keyboard(tg_id))

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id) == "waiting_category_name")
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
        bot.send_message(message.chat.id, "اختر من القائمة:", reply_markup=main_menu_keyboard(message.from_user.id))
    except sqlite3.IntegrityError:
        bot.reply_to(message, "❌ هذا التصنيف موجود مسبقاً.")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    tg_id = message.from_user.id
    state = user_states.get(tg_id, "")
    
    if state.startswith("waiting_app_file_"):
        if not is_admin(tg_id):
            return
        cat_id = int(state.split("_")[3])
        file_id = message.document.file_id
        file_name = message.document.file_name or "تطبيق"
        user_states[tg_id] = f"waiting_app_desc_{cat_id}_{file_id}_{file_name}"
        bot.reply_to(message, "✏️ أرسل الوصف:")
        return
    
    elif state == "waiting_upload_file":
        required = int(get_setting('referrals_for_feature')) * 0.5
        user = get_user(tg_id)
        if not user or user[2] < required:
            bot.reply_to(message, "❌ رصيدك غير كافٍ.")
            user_states.pop(tg_id, None)
            return
        update_balance(tg_id, -required)
        file_id = message.document.file_id
        file_name = message.document.file_name or "تطبيق"
        user_states[tg_id] = f"waiting_upload_desc_{file_id}_{file_name}"
        bot.reply_to(message, "✏️ أرسل الوصف:")
        return
    
    elif state == "waiting_vip_file":
        if not is_admin(tg_id):
            return
        file_id = message.document.file_id
        file_name = message.document.file_name or "VIP"
        user_states[tg_id] = f"waiting_vip_desc_{file_id}_{file_name}"
        bot.reply_to(message, "✏️ أرسل وصف VIP:")
        return

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id) == "waiting_search_query")
def receive_search_query(message):
    tg_id = message.from_user.id
    query = message.text.strip()
    if len(query) < 2:
        bot.reply_to(message, "❌ اكتب كلمة بحث أطول من حرفين.")
        return
    apps, total = search_apps(query)
    if not apps:
        bot.reply_to(message, "❌ لا توجد نتائج.")
        user_states.pop(tg_id, None)
        return
    keyboard = InlineKeyboardMarkup(row_width=1)
    for app_id, name, desc, file_id, cat_id in apps[:10]:
        keyboard.add(InlineKeyboardButton(f"📱 {name}", callback_data=f"app_{app_id}"))
    keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
    bot.reply_to(message, f"🔍 نتائج البحث عن '{query}' ({len(apps)}):", reply_markup=keyboard)
    user_states.pop(tg_id, None)

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, "").startswith("waiting_app_desc_"))
def receive_app_desc(message):
    tg_id = message.from_user.id
    state = user_states[tg_id]
    parts = state.split("_")
    cat_id = int(parts[3])
    file_id = parts[4]
    file_name = "_".join(parts[5:])
    description = message.text.strip()
    if not description:
        bot.reply_to(message, "الوصف لا يمكن أن يكون فارغاً.")
        return
    try:
        conn = db_conn()
        c = conn.cursor()
        c.execute("INSERT INTO apps (category_id, name, description, file_id, admin_id, added_date) VALUES (?, ?, ?, ?, ?, ?)",
                  (cat_id, file_name, description, file_id, ADMIN_ID, datetime.datetime.now().isoformat()))
        conn.commit()
        conn.close()
        log_admin_action(tg_id, "add_app", f"{file_name} في تصنيف {cat_id}")
        bot.reply_to(message, f"✅ تم إضافة '{file_name}'!")
        user_states.pop(tg_id, None)
        bot.send_message(tg_id, "اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
    except Exception as e:
        bot.reply_to(message, f"❌ خطأ: {e}")

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, "").startswith("waiting_upload_desc_"))
def receive_upload_desc(message):
    tg_id = message.from_user.id
    state = user_states[tg_id]
    parts = state.split("_")
    file_id = parts[3]
    file_name = "_".join(parts[4:])
    description = message.text
    conn = db_conn()
    c = conn.cursor()
    c.execute("INSERT INTO user_requests (user_id, type, app_name, description, file_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (tg_id, 'upload', file_name, description, file_id, 'pending', datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()
    bot.reply_to(message, "✅ تم إرسال طلب الرفع للإدمن.")
    user_states.pop(tg_id, None)
    if is_admin(ADMIN_ID):
        bot.send_message(ADMIN_ID, f"📩 طلب رفع جديد من @{message.from_user.username}\nالتطبيق: {file_name}")

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, "").startswith("waiting_vip_desc_"))
def receive_vip_desc(message):
    tg_id = message.from_user.id
    state = user_states[tg_id]
    parts = state.split("_")
    file_id = parts[3]
    file_name = "_".join(parts[4:])
    desc = message.text
    try:
        conn = db_conn()
        c = conn.cursor()
        c.execute("INSERT INTO vip_apps (name, description, file_id, admin_id, added_date) VALUES (?, ?, ?, ?, ?)",
                  (file_name, desc, file_id, ADMIN_ID, datetime.datetime.now().isoformat()))
        conn.commit()
        conn.close()
        log_admin_action(tg_id, "add_vip_app", file_name)
        bot.reply_to(message, "✅ تم إضافة VIP!")
        user_states.pop(tg_id, None)
        bot.send_message(tg_id, "اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
    except Exception as e:
        bot.reply_to(message, f"خطأ: {e}")

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id) == "waiting_crack_request")
def receive_crack_request(message):
    tg_id = message.from_user.id
    required = int(get_setting('referrals_for_feature')) * 0.5
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
    if is_admin(ADMIN_ID):
        bot.send_message(ADMIN_ID, f"📩 طلب كسر جديد من @{message.from_user.username}\nالتطبيق: {message.text}")

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, "").startswith("edit_setting_"))
def edit_setting_value(message):
    tg_id = message.from_user.id
    if not is_admin(tg_id):
        return
    key = user_states[tg_id].split("_")[2]
    value = message.text.strip()
    try:
        float(value)
        set_setting(key, value)
        log_admin_action(tg_id, "edit_setting", f"{key} = {value}")
        bot.reply_to(message, f"✅ تم تحديث {key} إلى {value}")
        user_states.pop(tg_id, None)
        bot.send_message(tg_id, "اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
    except ValueError:
        bot.reply_to(message, "❌ القيمة يجب أن تكون رقماً.")

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, "").startswith("edit_app_name_"))
def edit_app_name(message):
    tg_id = message.from_user.id
    if not is_admin(tg_id):
        return
    app_id = int(user_states[tg_id].split("_")[3])
    new_name = message.text.strip()
    if not new_name:
        bot.reply_to(message, "الاسم لا يمكن أن يكون فارغاً.")
        return
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE apps SET name=? WHERE id=?", (new_name, app_id))
    conn.commit()
    conn.close()
    log_admin_action(tg_id, "edit_app", f"التطبيق {app_id} -> {new_name}")
    bot.reply_to(message, f"✅ تم تحديث الاسم إلى {new_name}")
    user_states.pop(tg_id, None)
    bot.send_message(tg_id, "اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))

# ======================= تشغيل البوت =======================
if __name__ == "__main__":
    bot.set_my_commands([telebot.types.BotCommand("start", "بدء البوت")])
    process_pending_referrals()
    print("✅ البوت يعمل بكامل طاقته...")

    while True:
        try:
            bot.polling(non_stop=True, timeout=30, long_polling_timeout=20, skip_pending=True)
        except Exception as e:
            print(f"⚠️ خطأ: {e}. إعادة المحاولة بعد 10 ثوان...")
            time.sleep(10)