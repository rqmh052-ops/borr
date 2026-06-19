import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
import datetime
import time
import json

# ======================= الثوابت الأساسية (لا تغيرها إلا للضرورة) =======================
BOT_TOKEN = "8737177889:AAGnnxZq9Yyptc1cdLTpEv5FoNbT5Jn8SQY"
ADMIN_ID = 8287678319
CHANNEL_ID = -1004325834135  # قناة التخزين الخاصة (لن تظهر للمستخدمين)
CHANNEL_URL = "https://t.me/Bayan_x777"  # للاشتراك الإجباري
# =======================================================================================

bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}  # حفظ حالة المستخدمين

# ======================= قاعدة البيانات =======================
def init_db():
    conn = sqlite3.connect('bot_data.db', check_same_thread=False)
    c = conn.cursor()
    
    # المستخدمون
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0,
        is_vip INTEGER DEFAULT 0, vip_activated_date TEXT, referrer_id INTEGER
    )''')
    
    # الإحالات
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER, referred_id INTEGER,
        status TEXT, created_at TEXT, completed_at TEXT, channel_checked_date TEXT
    )''')
    
    # منع الغش (الأزواج المحظورة)
    c.execute('''CREATE TABLE IF NOT EXISTS banned_pairs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user1_id INTEGER, user2_id INTEGER
    )''')
    
    # التصنيفات
    c.execute('''CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, admin_id INTEGER
    )''')
    
    # التطبيقات الأساسية (مع file_id بدلاً من رابط)
    c.execute('''CREATE TABLE IF NOT EXISTS apps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER, name TEXT,
        description TEXT, file_id TEXT, admin_id INTEGER, added_date TEXT
    )''')
    
    # تطبيقات VIP
    c.execute('''CREATE TABLE IF NOT EXISTS vip_apps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, description TEXT,
        file_id TEXT, admin_id INTEGER, added_date TEXT
    )''')
    
    # طلبات المستخدمين
    c.execute('''CREATE TABLE IF NOT EXISTS user_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, type TEXT,
        app_name TEXT, description TEXT, file_id TEXT, status TEXT,
        admin_feedback TEXT, created_at TEXT
    )''')
    
    # إعدادات النظام (قابلة للتعديل من الإدمن)
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )''')
    
    # إدراج الإعدادات الافتراضية إن لم تكن موجودة
    default_settings = {
        'referrals_for_feature': '2',
        'referrals_for_vip': '10',
        'referral_expire_days': '4',
        'grace_period_hours': '24'
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

# ======================= دوال مساعدة =======================
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
        c.execute("INSERT INTO users (tg_id, username, referrer_id) VALUES (?, ?, ?)",
                  (tg_id, username, referrer_id))
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

# ======================= واجهات الأزرار =======================
def main_menu_keyboard(tg_id):
    keyboard = InlineKeyboardMarkup(row_width=2)
    buttons = [
        InlineKeyboardButton("📂 التطبيقات", callback_data="show_categories"),
        InlineKeyboardButton("👑 VIP", callback_data="show_vip"),
        InlineKeyboardButton("💰 رصيدي", callback_data="show_balance"),
        InlineKeyboardButton("🔗 رابط الدعوة", callback_data="get_referral_link"),
        InlineKeyboardButton("📨 طلب كسر / رفع", callback_data="make_request")
    ]
    keyboard.add(*buttons[:2])
    keyboard.add(*buttons[2:4])
    keyboard.add(buttons[4])
    if is_admin(tg_id):
        keyboard.add(InlineKeyboardButton("⚙️ لوحة الإدارة", callback_data="admin_panel"))
    return keyboard

def get_categories():
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT id, name FROM categories ORDER BY name")
    data = c.fetchall()
    conn.close()
    return data

def get_apps_by_category(category_id, page=1, per_page=5):
    conn = db_conn()
    c = conn.cursor()
    offset = (page - 1) * per_page
    c.execute("SELECT id, name, description, file_id FROM apps WHERE category_id=? LIMIT ? OFFSET ?",
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
    c.execute("SELECT id, name, description, file_id FROM vip_apps LIMIT ? OFFSET ?", (per_page, offset))
    apps = c.fetchall()
    c.execute("SELECT COUNT(*) FROM vip_apps")
    total = c.fetchone()[0]
    conn.close()
    return apps, total

# ======================= معالج واحد للأزرار (المركزي) =======================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    try:
        data = call.data
        tg_id = call.from_user.id
        message_id = call.message.message_id
        chat_id = call.message.chat.id
        
        # التحقق من الاشتراك الإجباري في كل تفاعل (ما عدا start)
        if data not in ['check_subscription', 'dummy']:
            if not check_subscription(tg_id):
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("📢 اشترك في القناة", url=CHANNEL_URL))
                keyboard.add(InlineKeyboardButton("🔄 تأكد من الاشتراك", callback_data="check_subscription"))
                bot.edit_message_text("⚠️ يجب عليك الاشتراك في القناة لاستخدام البوت:", chat_id, message_id, reply_markup=keyboard)
                return
        
        # --------------------- القائمة الرئيسية ---------------------
        if data == "main_menu":
            bot.edit_message_text("اختر من القائمة:", chat_id, message_id, reply_markup=main_menu_keyboard(tg_id))
        
        # --------------------- الاشتراك ---------------------
        elif data == "check_subscription":
            if check_subscription(tg_id):
                bot.edit_message_text("✅ تم التأكيد، مرحباً بك!", chat_id, message_id)
                bot.send_message(tg_id, "اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
            else:
                bot.answer_callback_query(call.id, "❌ لم تشترك بعد، اشترك ثم اضغط تأكيد.", show_alert=True)
        
        # --------------------- التصنيفات ---------------------
        elif data == "show_categories":
            categories = get_categories()
            keyboard = InlineKeyboardMarkup(row_width=1)
            for cat_id, name in categories:
                keyboard.add(InlineKeyboardButton(f"📁 {name}", callback_data=f"cat_{cat_id}"))
            if is_admin(tg_id):
                keyboard.add(InlineKeyboardButton("➕ إضافة تصنيف جديد", callback_data="add_category"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text("📂 اختر التصنيف:", chat_id, message_id, reply_markup=keyboard)
        
        elif data == "add_category":
            if not is_admin(tg_id):
                bot.answer_callback_query(call.id, "غير مصرح لك.")
                return
            user_states[tg_id] = "waiting_category_name"
            bot.edit_message_text("✏️ أرسل اسم التصنيف الجديد:", chat_id, message_id)
        
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
                nav_buttons.append(InlineKeyboardButton("⏪ السابق", callback_data=f"cat_{cat_id}_page_{page-1}"))
            if page * 5 < total:
                nav_buttons.append(InlineKeyboardButton("التالي ⏩", callback_data=f"cat_{cat_id}_page_{page+1}"))
            if nav_buttons:
                keyboard.add(*nav_buttons)
            for app_id, name, desc, file_id in apps:
                keyboard.add(InlineKeyboardButton(f"📱 {name}", callback_data=f"app_{app_id}"))
            if not apps:
                keyboard.add(InlineKeyboardButton("📭 لا توجد تطبيقات", callback_data="dummy"))
            if is_admin(tg_id):
                keyboard.add(InlineKeyboardButton("➕ إضافة تطبيق", callback_data=f"add_app_to_{cat_id}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_categories"))
            cat_name = dict(get_categories()).get(cat_id, "تصنيف")
            bot.edit_message_text(f"📂 {cat_name} - الصفحة {page}", chat_id, message_id, reply_markup=keyboard)
        
        elif data.startswith("add_app_to_"):
            if not is_admin(tg_id):
                bot.answer_callback_query(call.id, "غير مصرح.")
                return
            cat_id = int(data.split("_")[3])
            user_states[tg_id] = f"waiting_app_file_{cat_id}"
            bot.edit_message_text("📤 أرسل ملف التطبيق (APK) الآن، ثم أرسل الوصف في الرسالة التالية:", chat_id, message_id)
        
        elif data.startswith("app_"):
            app_id = int(data.split("_")[1])
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT name, description, file_id FROM apps WHERE id=?", (app_id,))
            app = c.fetchone()
            conn.close()
            if app:
                name, desc, file_id = app
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("📥 تحميل التطبيق", callback_data=f"download_app_{file_id}_{name}"))
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_categories"))
                bot.edit_message_text(f"📱 *{name}*\n\n{desc}", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
            else:
                bot.answer_callback_query(call.id, "التطبيق غير موجود")
        
        elif data.startswith("download_app_"):
            # زر التحميل الداخلي: يرسل الملف مباشرة بدون فتح القناة
            parts = data.split("_")
            file_id = parts[2]
            name = "_".join(parts[3:])
            try:
                bot.send_document(tg_id, file_id, caption=f"📱 {name}")
                bot.answer_callback_query(call.id, "✅ تم الإرسال")
            except Exception as e:
                bot.answer_callback_query(call.id, "❌ حدث خطأ في التحميل", show_alert=True)
                bot.send_message(ADMIN_ID, f"خطأ في إرسال الملف: {e}")
        
        # --------------------- VIP ---------------------
        elif data == "show_vip":
            user = get_user(tg_id)
            if not user or not user[3]:
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("🔓 شراء VIP", callback_data="buy_vip"))
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
                bot.edit_message_text("👑 *قسم VIP*\nحصري للأعضاء المميزين.\nللحصول على VIP تحتاج عدد معين من الإحالات.",
                                      chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
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
            for app_id, name, desc, file_id in apps:
                keyboard.add(InlineKeyboardButton(f"👑 {name}", callback_data=f"vip_app_{app_id}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text(f"👑 *التطبيقات الحصرية VIP* (صفحة {page})", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "buy_vip":
            required = int(get_setting('referrals_for_vip'))
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='completed'", (tg_id,))
            count = c.fetchone()[0]
            conn.close()
            if count >= required:
                set_vip(tg_id)
                bot.edit_message_text("🎉 *تهانينا! أصبحت عضواً مميزاً VIP مدى الحياة!*", chat_id, message_id, parse_mode='Markdown')
                bot.send_message(tg_id, "اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
            else:
                bot.answer_callback_query(call.id, f"تحتاج {required} إحالة، لديك {count}.", show_alert=True)
        
        elif data.startswith("vip_app_"):
            app_id = int(data.split("_")[2])
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT name, description, file_id FROM vip_apps WHERE id=?", (app_id,))
            app = c.fetchone()
            conn.close()
            if app:
                name, desc, file_id = app
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("📥 تحميل VIP", callback_data=f"download_vip_{file_id}_{name}"))
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_vip"))
                bot.edit_message_text(f"👑 *{name}*\n\n{desc}", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data.startswith("download_vip_"):
            parts = data.split("_")
            file_id = parts[2]
            name = "_".join(parts[3:])
            try:
                bot.send_document(tg_id, file_id, caption=f"👑 VIP: {name}")
                bot.answer_callback_query(call.id, "✅ تم الإرسال")
            except Exception as e:
                bot.answer_callback_query(call.id, "❌ خطأ في التحميل", show_alert=True)
                bot.send_message(ADMIN_ID, f"خطأ VIP: {e}")
        
        # --------------------- الرصيد والإحالات ---------------------
        elif data == "show_balance":
            user = get_user(tg_id)
            if not user:
                return
            balance = user[2]
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='completed'", (tg_id,))
            refs = c.fetchone()[0]
            conn.close()
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔗 رابط الدعوة", callback_data="get_referral_link"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text(f"💰 *رصيدك:* {balance} دولار\n📊 *عدد الإحالات الناجحة:* {refs}\n\n"
                                  f"كل إحالة = 0.5 دولار.\n"
                                  f"- {get_setting('referrals_for_feature')} إحالات = طلب كسر أو رفع.\n"
                                  f"- {get_setting('referrals_for_vip')} إحالات = VIP مدى الحياة.",
                                  chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "get_referral_link":
            link = f"https://t.me/{bot.get_me().username}?start=ref_{tg_id}"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text(f"🔗 *رابط دعوتك الشخصي:*\n`{link}`\n\nشاركه مع أصدقائك لكسب الإحالات.",
                                  chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
        # --------------------- طلب كسر / رفع ---------------------
        elif data == "make_request":
            required = int(get_setting('referrals_for_feature')) * 0.5  # نصف دولار لكل إحالة
            user = get_user(tg_id)
            if not user or user[2] < required:
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("🔗 جلب إحالات", callback_data="get_referral_link"))
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
                bot.edit_message_text(f"❌ رصيدك غير كافٍ (تحتاج {required} دولار = {get_setting('referrals_for_feature')} إحالات).",
                                      chat_id, message_id, reply_markup=keyboard)
                return
            keyboard = InlineKeyboardMarkup(row_width=2)
            keyboard.add(InlineKeyboardButton("🔨 طلب كسر", callback_data="request_crack"))
            keyboard.add(InlineKeyboardButton("📤 رفع تطبيق", callback_data="request_upload"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text("📨 اختر نوع الطلب:", chat_id, message_id, reply_markup=keyboard)
        
        elif data == "request_crack":
            user_states[tg_id] = "waiting_crack_request"
            bot.edit_message_text("✏️ أرسل اسم التطبيق الذي تريد كسره:", chat_id, message_id)
        
        elif data == "request_upload":
            user_states[tg_id] = "waiting_upload_file"
            bot.edit_message_text("📤 أرسل ملف التطبيق الذي تريد رفعه (مع وصف في الرسالة التالية):", chat_id, message_id)
        
        # --------------------- لوحة الإدمن ---------------------
        elif data == "admin_panel":
            if not is_admin(tg_id):
                bot.answer_callback_query(call.id, "غير مصرح.")
                return
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                InlineKeyboardButton("📊 تعديل الإعدادات", callback_data="edit_settings"),
                InlineKeyboardButton("➕ إضافة تصنيف", callback_data="add_category"),
                InlineKeyboardButton("📥 طلبات الموافقة", callback_data="view_requests"),
                InlineKeyboardButton("➕ إضافة تطبيق VIP", callback_data="add_vip_app"),
                InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")
            )
            bot.edit_message_text("⚙️ *لوحة الإدارة*", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "edit_settings":
            if not is_admin(tg_id):
                return
            settings = {
                'عدد الإحالات للطلب': 'referrals_for_feature',
                'عدد الإحالات للVIP': 'referrals_for_vip',
                'أيام صلاحية الإحالة': 'referral_expire_days',
                'ساعات المهلة بعد ترك القناة': 'grace_period_hours'
            }
            keyboard = InlineKeyboardMarkup(row_width=1)
            for label, key in settings.items():
                val = get_setting(key)
                keyboard.add(InlineKeyboardButton(f"{label}: {val}", callback_data=f"edit_setting_{key}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel"))
            bot.edit_message_text("📊 *الإعدادات الحالية*\nاختر إعداداً لتعديله:", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data.startswith("edit_setting_"):
            if not is_admin(tg_id):
                return
            key = data.split("_")[2]
            user_states[tg_id] = f"edit_setting_{key}"
            bot.edit_message_text(f"✏️ أرسل القيمة الجديدة للإعداد '{key}':", chat_id, message_id)
        
        elif data == "view_requests":
            if not is_admin(tg_id):
                return
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT id, user_id, type, app_name, status FROM user_requests WHERE status='pending'")
            reqs = c.fetchall()
            conn.close()
            if not reqs:
                bot.answer_callback_query(call.id, "لا توجد طلبات معلقة.")
                return
            keyboard = InlineKeyboardMarkup(row_width=2)
            for req_id, user_id, typ, name, status in reqs:
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
            c.execute("SELECT id, user_id, type, app_name, description, file_id FROM user_requests WHERE id=?", (req_id,))
            req = c.fetchone()
            conn.close()
            if not req:
                bot.answer_callback_query(call.id, "الطلب غير موجود.")
                return
            keyboard = InlineKeyboardMarkup(row_width=2)
            keyboard.add(InlineKeyboardButton("✅ موافقة", callback_data=f"approve_req_{req_id}"))
            keyboard.add(InlineKeyboardButton("❌ رفض", callback_data=f"reject_req_{req_id}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="view_requests"))
            text = f"طلب #{req_id}\nالمستخدم: {req[1]}\nالنوع: {req[2]}\nالاسم: {req[3]}\nالوصف: {req[4] or 'لا يوجد'}"
            if req[5]:
                text += f"\nFile ID: {req[5]}"
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
                bot.send_message(user_id, f"✅ تم الموافقة على طلب كسر تطبيق '{name}'.")
            else:  # upload
                try:
                    # نضيف التطبيق إلى أول تصنيف موجود
                    c.execute("SELECT id FROM categories LIMIT 1")
                    cat = c.fetchone()
                    if cat:
                        c.execute("INSERT INTO apps (category_id, name, description, file_id, admin_id, added_date) VALUES (?, ?, ?, ?, ?, ?)",
                                  (cat[0], name, desc, file_id, ADMIN_ID, datetime.datetime.now().isoformat()))
                        bot.send_message(user_id, f"✅ تم نشر تطبيق '{name}' بنجاح!")
                except Exception as e:
                    bot.send_message(ADMIN_ID, f"خطأ في النشر: {e}")
            c.execute("UPDATE user_requests SET status='approved', admin_feedback='تمت الموافقة' WHERE id=?", (req_id,))
            conn.commit()
            conn.close()
            bot.edit_message_text("✅ تمت الموافقة على الطلب.", chat_id, message_id)
            bot.send_message(tg_id, "اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
        
        elif data.startswith("reject_req_"):
            if not is_admin(tg_id):
                return
            req_id = int(data.split("_")[2])
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT user_id FROM user_requests WHERE id=?", (req_id,))
            user_id = c.fetchone()
            if user_id:
                update_balance(user_id[0], 1.0)
                bot.send_message(user_id[0], "❌ تم رفض طلبك، وتم إعادة الرصيد.")
            c.execute("UPDATE user_requests SET status='rejected', admin_feedback='تم الرفض' WHERE id=?", (req_id,))
            conn.commit()
            conn.close()
            bot.edit_message_text("❌ تم رفض الطلب.", chat_id, message_id)
            bot.send_message(tg_id, "اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
        
        elif data == "add_vip_app":
            if not is_admin(tg_id):
                return
            user_states[tg_id] = "waiting_vip_file"
            bot.edit_message_text("📤 أرسل ملف تطبيق VIP، ثم الوصف في الرسالة التالية:", chat_id, message_id)
        
        elif data == "dummy":
            bot.answer_callback_query(call.id)
        
        else:
            bot.answer_callback_query(call.id, "زر غير معروف")
    
    except Exception as e:
        print(f"Error in callback: {e}")
        bot.answer_callback_query(call.id, "حدث خطأ، حاول مجدداً.", show_alert=True)

# ======================= معالجات الرسائل النصية والملفات =======================
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
        bot.send_message(tg_id, f"✅ تم تسجيلك عن طريق دعوة! سيتم احتساب الإحالة بعد {get_setting('referral_expire_days')} أيام.")
    
    process_pending_referrals()
    
    if not check_subscription(tg_id):
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("📢 اشترك في القناة", url=CHANNEL_URL))
        keyboard.add(InlineKeyboardButton("🔄 تأكد من الاشتراك", callback_data="check_subscription"))
        bot.send_message(tg_id, "⚠️ يجب عليك الاشتراك في القناة لاستخدام البوت:", reply_markup=keyboard)
        return
    
    bot.send_message(tg_id, "مرحباً بك في بوت التطبيقات المكسورة 🚀", reply_markup=main_menu_keyboard(tg_id))

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
        c.execute("INSERT INTO categories (name, admin_id) VALUES (?, ?)", (name, message.from_user.id))
        conn.commit()
        conn.close()
        bot.reply_to(message, f"✅ تم إضافة التصنيف: {name}")
        user_states.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, "اختر من القائمة:", reply_markup=main_menu_keyboard(message.from_user.id))
    except sqlite3.IntegrityError:
        bot.reply_to(message, "❌ هذا التصنيف موجود مسبقاً.")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    tg_id = message.from_user.id
    state = user_states.get(tg_id, "")
    
    # إضافة تطبيق عادي
    if state.startswith("waiting_app_file_"):
        if not is_admin(tg_id):
            return
        cat_id = int(state.split("_")[3])
        file_id = message.document.file_id
        file_name = message.document.file_name or "تطبيق"
        user_states[tg_id] = f"waiting_app_desc_{cat_id}_{file_id}_{file_name}"
        bot.reply_to(message, "✏️ أرسل الآن الوصف النصي لهذا التطبيق:")
        return
    
    # رفع تطبيق من مستخدم
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
        bot.reply_to(message, "✏️ أرسل وصف التطبيق الآن:")
        return
    
    # إضافة VIP
    elif state == "waiting_vip_file":
        if not is_admin(tg_id):
            return
        file_id = message.document.file_id
        file_name = message.document.file_name or "VIP"
        user_states[tg_id] = f"waiting_vip_desc_{file_id}_{file_name}"
        bot.reply_to(message, "✏️ أرسل وصف تطبيق VIP:")
        return

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
        # تخزين في قاعدة البيانات مباشرة (بدون إرسال للقناة)
        conn = db_conn()
        c = conn.cursor()
        c.execute("INSERT INTO apps (category_id, name, description, file_id, admin_id, added_date) VALUES (?, ?, ?, ?, ?, ?)",
                  (cat_id, file_name, description, file_id, ADMIN_ID, datetime.datetime.now().isoformat()))
        conn.commit()
        conn.close()
        bot.reply_to(message, f"✅ تم إضافة التطبيق '{file_name}' بنجاح!")
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
    bot.reply_to(message, "✅ تم إرسال طلب الرفع للإدمن للموافقة.")
    user_states.pop(tg_id, None)
    if is_admin(ADMIN_ID):
        bot.send_message(ADMIN_ID, f"📩 طلب رفع جديد من {message.from_user.username}\nالتطبيق: {file_name}")

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
        bot.reply_to(message, "✅ تم إضافة تطبيق VIP بنجاح!")
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
    bot.reply_to(message, "✅ تم إرسال طلب الكسر للإدمن للموافقة.")
    user_states.pop(tg_id, None)
    if is_admin(ADMIN_ID):
        bot.send_message(ADMIN_ID, f"📩 طلب كسر جديد من {message.from_user.username}\nالتطبيق: {message.text}")

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, "").startswith("edit_setting_"))
def edit_setting_value(message):
    tg_id = message.from_user.id
    if not is_admin(tg_id):
        return
    key = user_states[tg_id].split("_")[2]
    value = message.text.strip()
    try:
        # التأكد من أن القيمة رقمية
        float(value)
        set_setting(key, value)
        bot.reply_to(message, f"✅ تم تحديث {key} إلى {value}")
        user_states.pop(tg_id, None)
        bot.send_message(tg_id, "اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
    except ValueError:
        bot.reply_to(message, "❌ القيمة يجب أن تكون رقماً.")

# ======================= تشغيل البوت =======================
if __name__ == "__main__":
    bot.set_my_commands([telebot.types.BotCommand("start", "بدء البوت")])
    process_pending_referrals()
    print("✅ البوت يعمل بكامل طاقته...")
    
    while True:
        try:
            bot.polling(non_stop=True, timeout=60, long_polling_timeout=30, skip_pending=True)
        except Exception as e:
            print(f"⚠️ خطأ: {e}. إعادة المحاولة بعد 10 ثوان...")
            time.sleep(10)