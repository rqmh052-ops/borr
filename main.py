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
# ⚠️ أمان: لا تكتب أي قيمة حساسة هنا مباشرة. كل القيم تُقرأ من متغيرات
# البيئة (Environment Variables) التي تُضبط من لوحة تحكم الاستضافة
# (مثل Railway -> Variables)، ولا تُكتب أبداً داخل الكود أو تُرفع لأي
# مستودع (git). هذا يمنع تسرب التوكن في حال كان الكود علنياً أو مشارَكاً.

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"❌ متغير البيئة '{name}' غير موجود. أضِفه من إعدادات الاستضافة "
            f"(Variables) قبل تشغيل البوت."
        )
    return value

def _require_env_int(name: str) -> int:
    value = _require_env(name)
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"❌ متغير البيئة '{name}' يجب أن يكون رقماً صحيحاً.")

BOT_TOKEN = _require_env("BOT_TOKEN")
ADMIN_ID = _require_env_int("ADMIN_ID")
FORCE_SUB_CHANNEL_ID = _require_env_int("FORCE_SUB_CHANNEL_ID")    # قناة الاشتراك الإجباري
FORCE_SUB_CHANNEL_URL = _require_env("FORCE_SUB_CHANNEL_URL")
DB_CHANNEL_ID = _require_env_int("DB_CHANNEL_ID")                  # قناة قاعدة البيانات
# =====================================================

bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}
pending_app_messages = {}   # يخزن مؤقتاً رسالة الملف الأصلية من الإدمن
pending_db_restores = {}    # يخزن مؤقتاً مسار ملف .db المرفوع بانتظار تأكيد الاستبدال

# يخزن مؤقتاً message_id لآخر رسالة "يجب الاشتراك" أُرسلت لكل مستخدم، بحيث لو
# كرّر /start ضغطاً عدة مرات دون الاشتراك، تُحذف الرسالة القديمة ويُرسل تنبيه
# جديد بدل تكديس عدة رسائل مطالبة بالاشتراك في الدردشة.
last_force_sub_prompt = {}

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
    ينشئ تذكرة عشوائية بحتة (ticket) صالحة لمرة واحدة ولمدة 60 ثانية.
    التذكرة لا تحمل أي معلومة قابلة للقراءة أو الاستنتاج (لا user_id ولا app_code
    ولا أي اشتقاق رياضي منهما) - فقط معرّف عشوائي 100% (secrets.token_urlsafe).
    كل الربط الفعلي (من يملك حق تحميل أي تطبيق) يبقى مخزّناً في الذاكرة فقط
    ولا يظهر إطلاقاً في نص الزر (callback_data) الذي قد يراه أي شخص بالضغط المطوّل.
    """
    expires = int(time.time()) + 60
    ticket = secrets.token_urlsafe(12)  # عشوائي بحت، لا علاقة له بأي بيانات حقيقية
    pending_tokens[ticket] = {
        "expires": expires,
        "app_code": app_code,
        "user_id": user_id
    }
    return ticket

def validate_token(ticket: str, user_id: int) -> str | None:
    """
    يتحقق من التذكرة العشوائية ويعيد app_code إذا كانت صالحة لنفس المستخدم الذي طلبها.
    تُحذف التذكرة فور استخدامها (one-time use) أو فور انتهاء صلاحيتها.
    """
    data = pending_tokens.get(ticket)
    if not data:
        return None
    if time.time() > data["expires"]:
        pending_tokens.pop(ticket, None)
        return None
    if data["user_id"] != user_id:
        # التذكرة موجودة لكنها مُصدرة لمستخدم آخر - لا نحذفها هنا حتى لا يتمكن
        # مهاجم يخمّن تذاكر عشوائية من إفناء تذاكر مستخدمين آخرين (DoS بسيط)
        return None
    app_code = data["app_code"]
    pending_tokens.pop(ticket, None)  # استخدام لمرة واحدة
    return app_code

# تخزين مؤقت في الذاكرة للـ tokens
pending_tokens = {}

# ======================= حماية ضد التخمين المتكرر (Rate Limiting) =======================
# ⚠️ هذا يحمي تحديداً مسار appv_{app_code}: لا يوجد طول مساحة كافٍ (7 أرقام
# فقط) ليكون التخمين المنهجي مستحيلاً عملياً، خصوصاً أن callback_query يمكن
# إرساله ببيانات تعسفية عبر مكتبات مثل Telethon/Pyrogram بغض النظر عن الزر
# الظاهر فعلياً. هذا حد بسيط في الذاكرة (غير حرج كفاية لتبرير تعقيد قاعدة
# بيانات إضافي) يوقف المحاولات الآلية المتلاحقة دون التأثير على الاستخدام
# الطبيعي العادي.
_failed_code_attempts = {}   # tg_id -> {"count": int, "blocked_until": float}
_RATE_LIMIT_MAX_ATTEMPTS = 5
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_BLOCK_SECONDS = 60

def is_rate_limited(tg_id: int) -> bool:
    """يتحقق هل المستخدم محظور مؤقتاً بسبب محاولات app_code خاطئة متكررة."""
    entry = _failed_code_attempts.get(tg_id)
    if not entry:
        return False
    if entry.get("blocked_until", 0) > time.time():
        return True
    return False

def register_failed_code_attempt(tg_id: int):
    """يسجّل محاولة app_code فاشلة، ويحظر المستخدم مؤقتاً عند تجاوز الحد."""
    now = time.time()
    entry = _failed_code_attempts.get(tg_id)
    if not entry or now - entry.get("window_start", 0) > _RATE_LIMIT_WINDOW_SECONDS:
        entry = {"count": 0, "window_start": now, "blocked_until": 0}
    entry["count"] += 1
    if entry["count"] >= _RATE_LIMIT_MAX_ATTEMPTS:
        entry["blocked_until"] = now + _RATE_LIMIT_BLOCK_SECONDS
    _failed_code_attempts[tg_id] = entry

def reset_failed_code_attempts(tg_id: int):
    """يصفّر العداد عند نجاح محاولة صحيحة."""
    _failed_code_attempts.pop(tg_id, None)

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
        is_banned INTEGER DEFAULT 0,
        frozen_balance REAL DEFAULT 0,
        violation_count INTEGER DEFAULT 0,
        is_frozen INTEGER DEFAULT 0
    )''')

    # إضافة الأعمدة الجديدة للمستخدمين القدامى إن لم تكن موجودة
    for col, definition in [
        ("frozen_balance", "REAL DEFAULT 0"),
        ("violation_count", "INTEGER DEFAULT 0"),
        ("is_frozen", "INTEGER DEFAULT 0"),
    ]:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
        except Exception:
            pass

    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER NOT NULL,
        referred_id INTEGER NOT NULL,
        status TEXT DEFAULT 'pending',
        created_at TEXT,
        completed_at TEXT,
        channel_checked_date TEXT,
        monitoring_start_at TEXT,
        UNIQUE(referrer_id, referred_id)
    )''')

    # إضافة العمود للجداول القديمة
    try:
        c.execute("ALTER TABLE referrals ADD COLUMN monitoring_start_at TEXT")
    except Exception:
        pass

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

    # ===================== أكواد الإحالة المختصرة (4 أرقام) =====================
    # يحلّ هذا الجدول محل صيغة ref_{tg_id} الطويلة بالكامل. كل مستخدم يحصل على
    # كود واحد ثابت من 4 أرقام عند أول استخدام، يُستعمل بدل الـ tg_id الكامل
    # داخل رابط /start. لا حاجة للحفاظ على الصيغة القديمة الطويلة.
    c.execute('''CREATE TABLE IF NOT EXISTS referral_codes (
        code TEXT PRIMARY KEY,
        tg_id INTEGER UNIQUE NOT NULL,
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

# ======================= نسخ احتياطي تلقائي لقاعدة البيانات الكاملة (قناة DB) =======================
# ⚠️ خط أحمر: هذا النظام يتعامل فقط مع رسالة النسخة الاحتياطية المحفوظ message_id
# الخاص بها في settings تحت المفتاح BACKUP_MSG_ID_KEY. لا يلمس أبداً أي رسالة
# أخرى في القناة (رسائل التطبيقات المخزنة في app_codes.channel_msg_id).
BACKUP_MSG_ID_KEY = "db_backup_message_id"
BACKUP_VERSION_KEY = "db_backup_version"
BACKUP_INTERVAL_SECONDS = 30 * 60  # 30 دقيقة

def _make_backup_snapshot() -> str:
    """
    ينشئ نسخة لقطة (snapshot) آمنة من قاعدة البيانات الكاملة باستخدام SQLite backup API
    (لا يقرأ الملف مباشرة لتفادي مشاكل WAL/التزامن)، ويعيد مسار الملف المؤقت.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    snapshot_path = os.path.join(BACKUP_DIR, f"snapshot_{ts}.db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(snapshot_path)
    src.backup(dst)
    dst.close()
    src.close()
    return snapshot_path

def _get_backup_state():
    """يقرأ message_id ورقم النسخة الحاليين لرسالة النسخة الاحتياطية من settings."""
    msg_id_raw = get_setting(BACKUP_MSG_ID_KEY)
    version_raw = get_setting(BACKUP_VERSION_KEY)
    msg_id = int(msg_id_raw) if msg_id_raw else None
    version = int(version_raw) if version_raw else 0
    return msg_id, version

def _save_backup_state(msg_id: int, version: int):
    """يحفظ message_id ورقم النسخة الجديدين في settings (نفس القاعدة، مفتاح مخصص)."""
    conn = db_conn()
    c = conn.cursor()
    c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (BACKUP_MSG_ID_KEY, str(msg_id)))
    c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (BACKUP_VERSION_KEY, str(version)))
    conn.commit()
    conn.close()

def send_or_update_db_backup():
    """
    يرسل/يحدّث رسالة النسخة الاحتياطية الكاملة لقاعدة البيانات في DB_CHANNEL_ID.
    - أول مرة: يرسل رسالة جديدة ويحفظ message_id الناتج.
    - المرات التالية: يحاول تعديل نفس الرسالة (editMessageMedia) برقم نسخة +1.
      لو فشل التعديل (مثلاً الرسالة حُذفت من القناة)، يحذف القديمة إن وُجدت،
      يرسل رسالة جديدة، ويثبّتها (Pin)، ويحفظ message_id الجديد.
    ⚠️ هذه الدالة لا تتعامل أبداً مع channel_msg_id الخاصة بالتطبيقات.
    """
    snapshot_path = None
    try:
        snapshot_path = _make_backup_snapshot()
        old_msg_id, old_version = _get_backup_state()
        new_version = old_version + 1
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        caption = (
            f"🗄️ *قاعدة البيانات الكاملة - نسخة احتياطية*\n"
            f"📦 النسخة (Version): #{new_version}\n"
            f"🕐 آخر تحديث: {ts}"
        )

        if old_msg_id:
            # نحاول التعديل على نفس الرسالة أولاً (الحل المفضل)
            try:
                with open(snapshot_path, 'rb') as f:
                    bot.edit_message_media(
                        chat_id=DB_CHANNEL_ID,
                        message_id=old_msg_id,
                        media=telebot.types.InputMediaDocument(
                            f,
                            caption=caption,
                            parse_mode='Markdown'
                        )
                    )
                _save_backup_state(old_msg_id, new_version)
                return
            except Exception as edit_err:
                print(f"⚠️ فشل تعديل رسالة النسخة الاحتياطية (msg_id={old_msg_id}): {edit_err}")
                # خط أحمر: نحذف فقط old_msg_id المحفوظ لدينا كرسالة نسخة احتياطية،
                # ولا نلمس أي رسالة تطبيقات أبداً.
                try:
                    bot.delete_message(DB_CHANNEL_ID, old_msg_id)
                except Exception as del_err:
                    print(f"⚠️ تعذّر حذف رسالة النسخة الاحتياطية القديمة: {del_err}")

        # إرسال رسالة جديدة (أول مرة، أو fallback بعد فشل التعديل)
        with open(snapshot_path, 'rb') as f:
            sent = bot.send_document(DB_CHANNEL_ID, f, caption=caption, parse_mode='Markdown')
        _save_backup_state(sent.message_id, new_version)
        # التثبيت مؤجّل حالياً بناءً على طلب صريح (الحد المجاني للتثبيت سينتهي قريباً)
        # عند تفعيله لاحقاً: bot.pin_chat_message(DB_CHANNEL_ID, sent.message_id, disable_notification=True)

    except Exception as e:
        print(f"⚠️ خطأ في النسخ الاحتياطي التلقائي للقناة: {e}")
    finally:
        if snapshot_path and os.path.exists(snapshot_path):
            try:
                os.remove(snapshot_path)
            except Exception:
                pass

def auto_backup():
    """يشغّل نسخة احتياطية كاملة كل 30 دقيقة تلقائياً بمجرد تشغيل البوت."""
    while True:
        try:
            send_or_update_db_backup()
        except Exception as e:
            print(f"⚠️ خطأ غير متوقع في auto_backup: {e}")
        time.sleep(BACKUP_INTERVAL_SECONDS)
# ملاحظة: يتم تشغيل هذا الـ Thread بعد تعريف get_setting/set_setting (انظر أسفل قسم الدوال المساعدة)

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

def escape_markdown(text: str) -> str:
    """
    يهرّب الرموز الخاصة بصيغة Markdown القديمة (legacy) التي يستخدمها البوت،
    لمنع كسر التنسيق عند استخدام اسم المستخدم (الذي قد يحتوي رموزاً) داخل نص منسّق.
    """
    if not text:
        return text
    for ch in ['_', '*', '`', '[']:
        text = text.replace(ch, f'\\{ch}')
    return text

def restore_database_from_file(uploaded_db_path: str):
    """
    يستبدل قاعدة البيانات الحالية بالكامل بملف .db مرفوع من الإدمن.
    خطوات أمان:
    1. التحقق أن الملف هو قاعدة SQLite صالحة (PRAGMA integrity_check).
    2. أخذ نسخة أمان من القاعدة الحالية قبل الاستبدال (في حال احتجنا التراجع).
    3. استبدال الملف الفعلي.
    لا تمسح هذه الدالة أي رسالة في قناة DB، فقط تتعامل مع الملف المحلي bot_data.db.
    """
    # 1) فحص سلامة الملف المرفوع
    test_conn = sqlite3.connect(uploaded_db_path)
    try:
        result = test_conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError("الملف المرفوع لا يبدو قاعدة بيانات SQLite صالحة (فشل integrity_check).")
    finally:
        test_conn.close()

    # 2) نسخة أمان من القاعدة الحالية قبل الاستبدال
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    pre_restore_backup = os.path.join(BACKUP_DIR, f"pre_restore_{ts}.db")
    if os.path.exists(DB_PATH):
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(pre_restore_backup)
        src.backup(dst)
        dst.close()
        src.close()

    # 3) الاستبدال الفعلي: نغلق أي اتصالات معلّقة، ثم ننسخ الملف الجديد فوق القديم
    #    (shutil.copyfile يحافظ على نفس المسار DB_PATH الذي تتصل به كل دوال البوت)
    for ext in ["", "-wal", "-shm"]:
        stale = DB_PATH + ext
        if ext and os.path.exists(stale):
            try:
                os.remove(stale)
            except Exception:
                pass
    shutil.copyfile(uploaded_db_path, DB_PATH)

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

def set_balance(tg_id, new_value: float):
    """يضبط رصيد المستخدم على قيمة محددة مباشرة (يُستخدم في تعديل الرصيد من لوحة الإدارة)."""
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = ? WHERE tg_id=?", (new_value, tg_id))
    conn.commit()
    conn.close()

def try_deduct_balance(tg_id, required: float) -> bool:
    """
    خصم ذرّي (atomic) للرصيد: الفحص والخصم يتمّان في استعلام SQL واحد،
    بدل قراءة الرصيد في بايثون ثم خصمه في خطوة منفصلة (TOCTOU race).
    لو وصلت طلبيتان متزامنتان لنفس المستخدم، فقط واحدة تنجح فعلياً
    (rowcount=1) والثانية تُرفض تلقائياً لأن الشرط balance>=required
    يُقيَّم في نفس عملية الكتابة الذرّية بواسطة SQLite.
    يُعيد True فقط إذا تم الخصم فعلياً.
    """
    conn = db_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE users SET balance = balance - ? WHERE tg_id=? AND balance >= ?",
        (required, tg_id, required)
    )
    success = c.rowcount > 0
    conn.commit()
    conn.close()
    return success

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

def force_sub_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("📢   اشترك في القناة   📢", url=FORCE_SUB_CHANNEL_URL))
    keyboard.add(InlineKeyboardButton("🔄   تأكد الاشتراك   🔄", callback_data="check_subscription"))
    return keyboard

def send_force_sub_prompt(tg_id):
    """
    يرسل رسالة "يجب الاشتراك" للمستخدم، ويحذف أي رسالة مطالبة سابقة كانت
    معلّقة له (لتفادي تكديس عدة رسائل متطابقة عند تكرار /start من مستخدم
    غير مشترك). هذا يُغلق الثغرة المطلوبة في نظام الاشتراك الإجباري.
    """
    old_msg_id = last_force_sub_prompt.get(tg_id)
    if old_msg_id:
        try:
            bot.delete_message(tg_id, old_msg_id)
        except Exception:
            pass  # الرسالة قد تكون محذوفة مسبقاً أو قديمة جداً للحذف، لا مشكلة

    sent = bot.send_message(tg_id, "⚠️ يجب الاشتراك في القناة أولاً لاستخدام البوت:",
                            reply_markup=force_sub_keyboard())
    last_force_sub_prompt[tg_id] = sent.message_id

# ======================= أكواد الإحالة المختصرة (4 أرقام) =======================
def get_referral_code(tg_id: int) -> str:
    """
    يعيد كود الإحالة المختصر (4 أرقام) الخاص بالمستخدم، وينشئ كوداً جديداً
    إن لم يكن لديه واحد بعد. الكود ثابت دائماً لنفس المستخدم بعد إنشائه.
    🔒 يُنشأ الكود عبر إعادة محاولة عشوائية حتى عدم التعارض (مساحة 10000
    احتمال كافية لعدد المستخدمين المتوقع لهذا النوع من البوتات).
    """
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT code FROM referral_codes WHERE tg_id=?", (tg_id,))
    row = c.fetchone()
    if row:
        conn.close()
        return row[0]

    for _ in range(50):
        code = f"{secrets.randbelow(10000):04d}"
        try:
            c.execute(
                "INSERT INTO referral_codes (code, tg_id, created_at) VALUES (?, ?, ?)",
                (code, tg_id, datetime.datetime.now().isoformat())
            )
            conn.commit()
            conn.close()
            return code
        except sqlite3.IntegrityError:
            # تعارض على الكود نفسه (نادر) أو على tg_id (سباق نادر بين طلبين
            # متزامنين لنفس المستخدم) - نتحقق من الحالة الثانية أولاً.
            c.execute("SELECT code FROM referral_codes WHERE tg_id=?", (tg_id,))
            existing = c.fetchone()
            if existing:
                conn.close()
                return existing[0]
            continue
    conn.close()
    raise RuntimeError("تعذّر إنشاء كود إحالة فريد بعد عدة محاولات.")

def resolve_referral_code(code: str) -> int | None:
    """يحوّل كود الإحالة المختصر إلى tg_id الخاص بالمُحيل، أو None إن لم يوجد."""
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT tg_id FROM referral_codes WHERE code=?", (code,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

MONITORING_HOURS = 2          # نافذة المراقبة بالساعات
DEDUCT_AMOUNT   = 0.25       # مبلغ الخصم عند المخالفة
VIOLATION_FREEZE = 5         # عدد المخالفات المتتالية للتجميد
REFERRAL_REWARD  = 0.5       # مكافأة الإحالة الفورية

def _user_link(tg_id: int, username: str | None) -> str:
    """ينشئ رابطاً أزرق قابلاً للضغط يفتح ملف المستخدم في تيليجرام."""
    display = f"@{username}" if username else f"مستخدم{tg_id}"
    return f'<a href="tg://user?id={tg_id}">{display}</a>'

def _get_username(tg_id: int) -> str | None:
    """يجلب اسم المستخدم من قاعدة البيانات."""
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT username FROM users WHERE tg_id=?", (tg_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def _add_violation(referrer_id: int, referred_id: int):
    """
    يسجل مخالفة على المحيل:
    - مخالفة 1       → تسجيل فقط
    - مخالفة 2,3,4   → خصم 0.25$
    - مخالفة 5       → تجميد الرصيد
    """
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT violation_count, balance, is_frozen FROM users WHERE tg_id=?", (referrer_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return
    v_count, balance, is_frozen = row
    if is_frozen:
        conn.close()
        return

    new_count = v_count + 1
    referred_username = _get_username(referred_id)
    ref_link = _user_link(referred_id, referred_username)

    if new_count == 1:
        # مخالفة أولى: تسجيل فقط
        c.execute("UPDATE users SET violation_count=? WHERE tg_id=?", (new_count, referrer_id))
        conn.commit()
        conn.close()
        try:
            bot.send_message(
                referrer_id,
                f"⚠️ خرج {ref_link} من القناة قبل اكتمال ساعتين.\n"
                f"تم تسجيل تحذير أول — لا خصم هذه المرة.",
                parse_mode='HTML'
            )
        except Exception:
            pass

    elif new_count < VIOLATION_FREEZE:
        # مخالفة 2-4: خصم 0.25$
        new_balance = max(0.0, balance - DEDUCT_AMOUNT)
        c.execute("UPDATE users SET violation_count=?, balance=? WHERE tg_id=?",
                  (new_count, new_balance, referrer_id))
        conn.commit()
        conn.close()
        try:
            bot.send_message(
                referrer_id,
                f"⚠️ خرج {ref_link} من القناة قبل اكتمال ساعتين.\n"
                f"تم خصم {DEDUCT_AMOUNT}$ من رصيدك بسبب تكرار الإحالات غير المكتملة.",
                parse_mode='HTML'
            )
        except Exception:
            pass

    else:
        # مخالفة 5: تجميد الرصيد
        c.execute("UPDATE users SET violation_count=?, frozen_balance=balance, balance=0, is_frozen=1 WHERE tg_id=?",
                  (new_count, referrer_id))
        conn.commit()
        conn.close()
        try:
            bot.send_message(
                referrer_id,
                f"⛔ تم تجميد رصيدك بسبب تكرار المخالفات المتتالية ({VIOLATION_FREEZE} مرات).\n"
                f"تواصل مع الإدارة لرفع التجميد.",
                parse_mode='HTML'
            )
        except Exception:
            pass

def _reset_violations(referrer_id: int):
    """يصفّر عداد المخالفات عند اكتمال إحالة ناجحة."""
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET violation_count=0 WHERE tg_id=?", (referrer_id,))
    conn.commit()
    conn.close()

# 🔒 قفل يمنع تشغيل process_pending_referrals من أكثر من thread في نفس اللحظة
_referrals_lock = threading.Lock()

def process_pending_referrals():
    """
    يعالج الإحالات المعلقة بالمنطق الجديد:
    ─ الإحالة تُحتسب فورًا عند تحقق الاشتراك (monitoring).
    ─ بعدها نافذة مراقبة ساعتين:
        • بقي  → إحالة ناجحة، عداد مخالفات = 0
        • خرج  → تسجيل مخالفة على المحيل
    """
    if not _referrals_lock.acquire(blocking=False):
        return
    try:
        conn = db_conn()
        c = conn.cursor()
        now = datetime.datetime.now()

        # جلب كل الإحالات المعلقة
        c.execute("""
            SELECT id, referrer_id, referred_id, status, monitoring_start_at
            FROM referrals
            WHERE status IN ('pending', 'monitoring')
        """)
        rows = c.fetchall()

        for ref_id, referrer_id, referred_id, status, monitoring_start in rows:

            subscribed = check_subscription(referred_id)

            # ── الحالة 1: pending ولم يشترك بعد ──────────────────────────
            if status == 'pending' and not subscribed:
                # لا نفعل شيئاً، ننتظر حتى يشترك
                continue

            # ── الحالة 2: pending واشترك → احتسب الإحالة فوراً ────────────
            if status == 'pending' and subscribed:
                monitoring_start_dt = now.isoformat()
                c.execute("""
                    UPDATE referrals
                    SET status='monitoring', monitoring_start_at=?
                    WHERE id=?
                """, (monitoring_start_dt, ref_id))
                conn.commit()

                # مكافأة فورية للمحيل
                update_balance(referrer_id, REFERRAL_REWARD)

                # إشعار المحيل باسم المدعو كرابط أزرق
                referred_username = _get_username(referred_id)
                ref_link = _user_link(referred_id, referred_username)
                try:
                    bot.send_message(
                        referrer_id,
                        f"✅ تم احتساب إحالة جديدة!\n"
                        f"👤 {ref_link}\n"
                        f"💰 +{REFERRAL_REWARD}$\n\n"
                        f"⏳ سيتم متابعة الإحالة لمدة ساعتين للتأكد من استقرارها.",
                        parse_mode='HTML'
                    )
                except Exception:
                    pass
                continue

            # ── الحالة 3: monitoring ──────────────────────────────────────
            if status == 'monitoring' and monitoring_start:
                start_dt = datetime.datetime.fromisoformat(monitoring_start)
                elapsed_hours = (now - start_dt).total_seconds() / 3600

                still_subscribed = check_subscription(referred_id)

                if not still_subscribed:
                    # خرج قبل ساعتين → مخالفة
                    c.execute("UPDATE referrals SET status='violated' WHERE id=?", (ref_id,))
                    conn.commit()
                    _add_violation(referrer_id, referred_id)

                elif elapsed_hours >= MONITORING_HOURS:
                    # اكتملت ساعتان وما زال مشتركاً → إحالة ناجحة نهائية
                    c.execute("""
                        UPDATE referrals
                        SET status='completed', completed_at=?
                        WHERE id=?
                    """, (now.isoformat(), ref_id))
                    c.execute("INSERT OR IGNORE INTO banned_pairs (user1_id, user2_id) VALUES (?, ?)",
                              (referrer_id, referred_id))
                    conn.commit()
                    _reset_violations(referrer_id)

                    referred_username = _get_username(referred_id)
                    ref_link = _user_link(referred_id, referred_username)
                    try:
                        bot.send_message(
                            referrer_id,
                            f"✅ تمت الإحالة بنجاح واستقرت بعد المراقبة!\n"
                            f"👤 {ref_link}",
                            parse_mode='HTML'
                        )
                    except Exception:
                        pass

        conn.close()
    finally:
        _referrals_lock.release()

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

def rename_app_by_code(app_code: str, new_name: str):
    """
    يعدّل الاسم الظاهر فقط (عمود name في app_codes) المستخدم في نتائج البحث
    والقوائم. لا يلمس أبداً اسم الملف الأصلي (file_name) ولا الرسالة المخزنة
    في قناة DB ولا الكود الداخلي (app_code) ولا channel_msg_id.
    """
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE app_codes SET name=? WHERE app_code=?", (new_name, app_code))
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

    # 🔒 طبقة دفاع ثانية (defense in depth): حتى لو وصلت تذكرة صالحة بأي شكل
    # لمستخدم غير VIP لتطبيق VIP، هذا الفحص النهائي يمنع التسليم الفعلي.
    if is_vip and not is_admin(tg_id):
        user = get_user(tg_id)
        if not (user and user[3]):
            return False

    try:
        # نسخ الرسالة من قناة قاعدة البيانات للمستخدم بدون إظهار "محوّل من"
        bot.copy_message(
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
        InlineKeyboardButton("💰 تجميع الرصيد", callback_data="show_balance"),
        InlineKeyboardButton("📨 طلب كسر / رفع", callback_data="make_request"),
        InlineKeyboardButton("🔍 بحث", callback_data="search_apps"),
    ]
    keyboard.add(*buttons[:2])
    keyboard.add(buttons[2])
    keyboard.add(buttons[3])
    keyboard.add(buttons[4])
    if is_admin(tg_id):
        keyboard.add(InlineKeyboardButton("⚙️ لوحة الإدارة", callback_data="admin_panel"))
    return keyboard

def admin_panel_keyboard():
    """
    لوحة الإدارة الموحّدة.
    """
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("📊 الإحصائيات", callback_data="admin_stats"),
        InlineKeyboardButton("📥 الطلبات المعلقة", callback_data="view_requests"),
        InlineKeyboardButton("📂 إدارة التصنيفات", callback_data="show_categories"),
        InlineKeyboardButton("👑 إدارة VIP", callback_data="show_vip"),
        InlineKeyboardButton("💳 تعديل رصيد مستخدم", callback_data="admin_edit_balance"),
        InlineKeyboardButton("🔓 رفع تجميد مستخدم", callback_data="admin_unfreeze"),
        InlineKeyboardButton("⚙️ الإعدادات", callback_data="edit_settings"),
        InlineKeyboardButton("👥 المستخدمين", callback_data="admin_users"),
        InlineKeyboardButton("📋 سجل الإدمن", callback_data="admin_logs"),
        InlineKeyboardButton("🔧 وضع الصيانة", callback_data="toggle_maintenance"),
        InlineKeyboardButton("💾 نسخ احتياطي", callback_data="backup_db"),
        InlineKeyboardButton("📤 رفع قاعدة بيانات", callback_data="upload_db_prompt"),
        InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")
    )
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
                bot.edit_message_text("⚠️ يجب الاشتراك في القناة أولاً لاستخدام البوت:",
                                      chat_id, message_id, reply_markup=force_sub_keyboard())
                return

        # ========== القائمة الرئيسية ==========
        if data == "main_menu":
            bot.edit_message_text("🏠 اختر من القائمة:", chat_id, message_id, reply_markup=main_menu_keyboard(tg_id))

        elif data == "check_subscription":
            if check_subscription(tg_id):
                last_force_sub_prompt.pop(tg_id, None)
                # تشغيل فوري لمعالجة الإحالة إن وُجدت
                threading.Thread(target=process_pending_referrals, daemon=True).start()
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

            # 🔒 حماية من تخمين app_code المتكرر (انظر شرح _failed_code_attempts بالأعلى)
            if is_rate_limited(tg_id):
                bot.answer_callback_query(call.id, "⏱️ محاولات كثيرة، حاول لاحقاً بعد دقيقة.", show_alert=True)
                return

            info = get_app_info(app_code)
            if not info:
                register_failed_code_attempt(tg_id)
                bot.answer_callback_query(call.id, "التطبيق غير موجود.")
                return
            reset_failed_code_attempts(tg_id)
            _, channel_msg_id, category, is_vip, name, downloads = info

            # 🔒 فحص صلاحية VIP الفعلي: is_vip في app_codes كان يُستخدم سابقاً فقط
            # لتصفية القوائم، دون منع التحميل المباشر. الآن نتحقق أن المستخدم نفسه
            # VIP فعلياً (أو إدمن) قبل السماح بإنشاء تذكرة تحميل لتطبيق VIP.
            if is_vip and not is_admin(tg_id):
                user = get_user(tg_id)
                user_is_vip = bool(user and user[3])
                if not user_is_vip:
                    keyboard = InlineKeyboardMarkup()
                    keyboard.add(InlineKeyboardButton("🔓 احصل على VIP", callback_data="buy_vip"))
                    keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_vip"))
                    bot.edit_message_text(
                        "👑 هذا تطبيق VIP حصري.\nيلزمك تفعيل VIP أولاً للوصول إليه.",
                        chat_id, message_id, reply_markup=keyboard
                    )
                    return

            # نُنشئ تذكرة عشوائية بحتة (لا تحمل أي معلومة قابلة للقراءة) صالحة لمرة واحدة لمدة 60 ثانية
            ticket = generate_user_token(tg_id, app_code)

            keyboard = InlineKeyboardMarkup(row_width=2)
            keyboard.add(InlineKeyboardButton(
                "📥 تحميل",
                callback_data=f"dl_{ticket}"
            ))
            if is_admin(tg_id):
                keyboard.add(
                    InlineKeyboardButton("✏️ تعديل الاسم", callback_data=f"renameapp_{app_code}"),
                    InlineKeyboardButton("🗑️ حذف", callback_data=f"delapp_{app_code}")
                )

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

        # ========== زر التحميل الفعلي (مع تذكرة عشوائية بحتة) ==========
        elif data.startswith("dl_"):
            ticket = data[3:]
            app_code = validate_token(ticket, tg_id)
            if not app_code:
                bot.answer_callback_query(call.id, "⏱️ انتهت صلاحية الزر، اضغط على التطبيق مرة أخرى.", show_alert=True)
                return

            success = deliver_app_to_user(app_code, tg_id)
            if success:
                bot.answer_callback_query(call.id, "✅ تم الإرسال!")
            else:
                bot.answer_callback_query(call.id, "❌ خطأ في الإرسال، تم إبلاغ الإدمن.", show_alert=True)

        # ========== تعديل اسم تطبيق (الاسم الظاهر فقط، اسم الملف يبقى كما هو) ==========
        elif data.startswith("renameapp_"):
            if not is_admin(tg_id):
                return
            app_code = data[len("renameapp_"):]
            info = get_app_info(app_code)
            if not info:
                bot.answer_callback_query(call.id, "التطبيق غير موجود.")
                return
            current_name = info[4]
            user_states[tg_id] = f"waiting_rename_app|{app_code}"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("❌ إلغاء", callback_data=f"appv_{app_code}"))
            bot.edit_message_text(
                f"✏️ الاسم الحالي: *{current_name}*\n\n"
                f"أرسل الاسم الجديد الذي سيظهر في نتائج البحث والقائمة فقط\n"
                f"(اسم الملف الأصلي لن يتغيّر):",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

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

        # ========== تجميع الرصيد (الرصيد + كود الإحالة في صفحة واحدة) ==========
        elif data == "show_balance":
            user = get_user(tg_id)
            if not user:
                return
            balance      = user[2]
            frozen_bal   = user[9] if len(user) > 9 else 0.0
            v_count      = user[10] if len(user) > 10 else 0
            is_frozen    = user[11] if len(user) > 11 else 0

            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='completed'", (tg_id,))
            refs = c.fetchone()[0]

            # الاشتراكات المعلقة (pending + monitoring)
            c.execute("""
                SELECT referred_id, status, monitoring_start_at
                FROM referrals
                WHERE referrer_id=? AND status IN ('pending','monitoring')
                ORDER BY id DESC LIMIT 5
            """, (tg_id,))
            pending_rows = c.fetchall()
            c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status IN ('pending','monitoring')", (tg_id,))
            pending_count = c.fetchone()[0]
            conn.close()

            ref_code = get_referral_code(tg_id)
            bot_username = bot.get_me().username
            ref_link = f"https://t.me/{bot_username}?start=ref_{ref_code}"

            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(InlineKeyboardButton("📤 مشاركة البوت", switch_inline_query=ref_link))

            # زر الاشتراكات المعلقة باللون الأزرق (عبر نص HTML وليس inline فقط)
            if pending_count > 0:
                keyboard.add(InlineKeyboardButton(
                    f"🔵 الاشتراكات المعلقة ({pending_count})",
                    callback_data="show_pending_referrals"
                ))

            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))

            frozen_line = f"\n❄️ *مجمد:* {frozen_bal:.2f}$" if is_frozen else ""
            warn_line   = f"\n⚠️ *تحذيرات متتالية:* {v_count}" if v_count > 0 else ""

            bot.edit_message_text(
                f"💰 *الرصيد الحالي:* {balance:.2f}$"
                f"{frozen_line}"
                f"\n📊 *الإحالات الناجحة:* {refs}"
                f"\n⏳ *معلقة:* {pending_count}"
                f"{warn_line}\n\n"
                f"كل إحالة = {REFERRAL_REWARD}$\n"
                f"• {get_setting('referrals_for_feature')} إحالات = طلب كسر/رفع\n"
                f"• {get_setting('referrals_for_vip')} إحالات = VIP\n\n"
                f"➖➖➖➖➖➖➖➖➖➖\n"
                f"🔗 *كود إحالتك:* `{ref_code}`\n"
                f"رابط دعوتك:\n`{ref_link}`",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        # ========== طلب كسر / رفع ==========
        elif data == "make_request":
            required = float(get_setting('referrals_for_feature') or 2) * 0.5
            user = get_user(tg_id)
            if not user or user[2] < required:
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("🔗 جلب إحالات", callback_data="show_balance"))
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

        # ========== رفع تجميد مستخدم (إدمن فقط) ==========
        elif data == "admin_unfreeze":
            if not is_admin(tg_id):
                return
            user_states[tg_id] = "waiting_unfreeze_user_id"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 إلغاء", callback_data="admin_panel"))
            bot.edit_message_text(
                "🔓 *رفع التجميد*\n\nأرسل User ID للمستخدم المراد رفع تجميده:",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        elif data == "admin_panel":
            if not is_admin(tg_id):
                return
            bot.edit_message_text("⚙️ *لوحة الإدارة*", chat_id, message_id,
                                  parse_mode='Markdown', reply_markup=admin_panel_keyboard())

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

        # ========== تعديل رصيد مستخدم عبر User ID فقط ==========
        elif data == "admin_edit_balance":
            if not is_admin(tg_id):
                return
            user_states[tg_id] = "waiting_balance_user_id"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 إلغاء", callback_data="admin_panel"))
            bot.edit_message_text(
                "💳 *تعديل رصيد مستخدم*\n\n"
                "أرسل الآن User ID (معرّف المستخدم الرقمي) فقط:",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        # ========== اختيار نوع تعديل الرصيد (إضافة / خصم / تعيين) ==========
        elif data.startswith("baladmin_op|"):
            if not is_admin(tg_id):
                return
            _, target_id_str, op = data.split("|")
            target_id = int(target_id_str)
            user = get_user(target_id)
            if not user:
                bot.answer_callback_query(call.id, "❌ المستخدم غير مسجل في البوت.", show_alert=True)
                return
            op_labels = {"add": "➕ إضافة", "sub": "➖ خصم", "set": "✏️ تعيين قيمة"}
            user_states[tg_id] = f"waiting_balance_amount|{target_id}|{op}"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 إلغاء", callback_data="admin_panel"))
            bot.edit_message_text(
                f"💳 المستخدم: `{target_id}`\n"
                f"الرصيد الحالي: {user[2]} دولار\n"
                f"العملية: {op_labels.get(op, op)}\n\n"
                f"أرسل القيمة (رقم):",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

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
            bot.edit_message_text("⚙️ *لوحة الإدارة*", chat_id, message_id,
                                  parse_mode='Markdown', reply_markup=admin_panel_keyboard())

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

        # ========== رفع/استرجاع قاعدة بيانات كاملة (يدوي فقط) ==========
        elif data == "upload_db_prompt":
            if not is_admin(tg_id):
                return
            user_states[tg_id] = "waiting_db_upload_file"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 إلغاء", callback_data="admin_panel"))
            bot.edit_message_text(
                "📤 *رفع قاعدة بيانات*\n\n"
                "أرسل الآن ملف قاعدة البيانات (.db) لاستعادته.\n\n"
                "⚠️ تحذير: هذا سيستبدل قاعدة البيانات الحالية بالكامل (المستخدمين، "
                "التطبيقات، الإحالات، كل شيء) بعد طلب تأكيد منك.",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        elif data == "confirm_db_restore":
            if not is_admin(tg_id):
                return
            pending = pending_db_restores.pop(tg_id, None)
            if not pending or not os.path.exists(pending):
                bot.answer_callback_query(call.id, "❌ انتهت صلاحية العملية، أعد رفع الملف.", show_alert=True)
                return
            try:
                restore_database_from_file(pending)
                log_admin_action(tg_id, "restore_db", f"تم استبدال القاعدة من ملف: {pending}")
                bot.edit_message_text("✅ تم استبدال قاعدة البيانات بالكامل بنجاح.", chat_id, message_id)
                bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
            except Exception as e:
                bot.edit_message_text(f"❌ فشل الاستبدال: {e}\n\nقاعدة البيانات القديمة لم تتأثر.", chat_id, message_id)
            finally:
                try:
                    if os.path.exists(pending):
                        os.remove(pending)
                except Exception:
                    pass

        elif data == "cancel_db_restore":
            if not is_admin(tg_id):
                return
            pending = pending_db_restores.pop(tg_id, None)
            if pending and os.path.exists(pending):
                try:
                    os.remove(pending)
                except Exception:
                    pass
            bot.edit_message_text("❌ تم إلغاء عملية الاستبدال. لم يتم تغيير أي شيء.", chat_id, message_id)
            bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))

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

        # ========== الاشتراكات المعلقة (آخر 5 فقط) ==========
        elif data == "show_pending_referrals":
            conn = db_conn()
            c = conn.cursor()
            c.execute("""
                SELECT referred_id, status, monitoring_start_at
                FROM referrals
                WHERE referrer_id=? AND status IN ('pending','monitoring')
                ORDER BY id DESC LIMIT 5
            """, (tg_id,))
            rows = c.fetchall()
            c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status IN ('pending','monitoring')", (tg_id,))
            total_pending = c.fetchone()[0]
            conn.close()

            if not rows:
                bot.answer_callback_query(call.id, "لا توجد اشتراكات معلقة حالياً.")
                return

            now = datetime.datetime.now()
            lines = []
            for referred_id_r, status_r, mon_start in rows:
                r_username = _get_username(referred_id_r)
                r_link = _user_link(referred_id_r, r_username)
                if status_r == 'monitoring' and mon_start:
                    start_dt = datetime.datetime.fromisoformat(mon_start)
                    elapsed = (now - start_dt).total_seconds() / 3600
                    remaining = max(0, MONITORING_HOURS - elapsed)
                    state_txt = f"⏳ مراقبة — باقي {remaining:.1f} ساعة"
                else:
                    state_txt = "🔄 في انتظار الاشتراك"
                lines.append(f"• {r_link} — {state_txt}")

            text = (
                f"🔵 <b>الاشتراكات المعلقة ({total_pending})</b>\n"
                f"<i>يعرض آخر 5 فقط</i>\n\n"
                + "\n".join(lines)
            )
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_balance"))
            bot.edit_message_text(text, chat_id, message_id, parse_mode='HTML', reply_markup=keyboard)

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

    # استخراج كود الإحالة إن وُجد
    referrer_id = None
    has_ref_payload = False
    if ' ' in message.text:
        payload = message.text.split(' ', 1)[1].strip()
        if payload.startswith('ref_'):
            ref_code = payload[4:]
            resolved = resolve_referral_code(ref_code)
            if resolved and resolved != tg_id:
                referrer_id = resolved
                has_ref_payload = True

    is_new = register_user(tg_id, username, referrer_id)

    # تشغيل معالج الإحالات في الخلفية
    threading.Thread(target=process_pending_referrals, daemon=True).start()

    if get_setting('maintenance_mode') == '1' and not is_admin(tg_id):
        bot.send_message(tg_id, "🔧 البوت في وضع الصيانة، عذراً.")
        return

    # ── دخول عبر رابط إحالة ──────────────────────────────────────────────
    if has_ref_payload:
        if not check_subscription(tg_id):
            # غير مشترك → رسالة الإحالة المخصصة (ليس رسالة الدخول العادي)
            old_msg_id = last_force_sub_prompt.get(tg_id)
            if old_msg_id:
                try:
                    bot.delete_message(tg_id, old_msg_id)
                except Exception:
                    pass
            sent = bot.send_message(
                tg_id,
                "📢 اشترك في القناة حتى يتم احتساب الإحالة:",
                reply_markup=force_sub_keyboard()
            )
            last_force_sub_prompt[tg_id] = sent.message_id
            # نسجل الإحالة كـ pending إن كان مستخدماً جديداً
            if is_new and referrer_id:
                add_referral(referrer_id, tg_id)
            return
        else:
            # مشترك → نسجل الإحالة إن لزم ونشغل المعالج
            if is_new and referrer_id:
                add_referral(referrer_id, tg_id)
            # تشغيل فوري لمعالجة الاشتراك الجديد
            threading.Thread(target=process_pending_referrals, daemon=True).start()
            last_force_sub_prompt.pop(tg_id, None)
            display_name = escape_markdown(message.from_user.first_name or username)
            name_link = f"[{display_name}](tg://user?id={tg_id})"
            bot.send_message(
                tg_id,
                f"مرحباً بك {name_link} 🚀",
                parse_mode='Markdown',
                reply_markup=main_menu_keyboard(tg_id)
            )
            return

    # ── دخول عادي (بدون رابط إحالة) ─────────────────────────────────────
    if not check_subscription(tg_id):
        send_force_sub_prompt(tg_id)
        return

    last_force_sub_prompt.pop(tg_id, None)
    display_name = escape_markdown(message.from_user.first_name or username)
    name_link = f"[{display_name}](tg://user?id={tg_id})"
    bot.send_message(
        tg_id,
        f"مرحباً بك {name_link} 🚀",
        parse_mode='Markdown',
        reply_markup=main_menu_keyboard(tg_id)
    )

# ========== استقبال ملف قاعدة البيانات (.db) المرفوع من الإدمن لاستعادتها ==========
@bot.message_handler(content_types=['document'],
                     func=lambda m: is_admin(m.from_user.id)
                     and user_states.get(m.from_user.id) == "waiting_db_upload_file")
def receive_db_upload(message):
    tg_id = message.from_user.id
    file_name = message.document.file_name or ""
    if not file_name.lower().endswith(".db"):
        bot.reply_to(message, "❌ الملف يجب أن يكون بصيغة .db فقط. أعد الإرسال أو اضغط إلغاء.")
        return
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        download_path = os.path.join(BACKUP_DIR, f"uploaded_{ts}.db")
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(download_path, 'wb') as f:
            f.write(downloaded)
    except Exception as e:
        bot.reply_to(message, f"❌ فشل تنزيل الملف: {e}")
        user_states.pop(tg_id, None)
        return

    # فحص سريع لسلامة الملف قبل عرض التأكيد
    try:
        check_conn = sqlite3.connect(download_path)
        result = check_conn.execute("PRAGMA integrity_check").fetchone()
        check_conn.close()
        if not result or result[0] != "ok":
            os.remove(download_path)
            bot.reply_to(message, "❌ الملف ليس قاعدة بيانات SQLite صالحة. لم يتم تغيير أي شيء.")
            user_states.pop(tg_id, None)
            return
    except Exception as e:
        if os.path.exists(download_path):
            os.remove(download_path)
        bot.reply_to(message, f"❌ تعذّر قراءة الملف كقاعدة بيانات: {e}")
        user_states.pop(tg_id, None)
        return

    pending_db_restores[tg_id] = download_path
    user_states.pop(tg_id, None)
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(InlineKeyboardButton("✅ تأكيد الاستبدال", callback_data="confirm_db_restore"))
    keyboard.add(InlineKeyboardButton("❌ إلغاء", callback_data="cancel_db_restore"))
    bot.reply_to(message,
        "⚠️ *تأكيد الاستبدال*\n\n"
        f"الملف `{file_name}` تم استلامه وفحصه بنجاح.\n\n"
        "هل أنت متأكد أنك تريد استبدال قاعدة البيانات الحالية بالكامل بهذا الملف؟\n"
        "(سيتم أخذ نسخة أمان من القاعدة الحالية تلقائياً قبل الاستبدال تحسباً لأي خطأ)",
        parse_mode='Markdown', reply_markup=keyboard
    )

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
    caption_text += f"\n\n[تابع القناة]({FORCE_SUB_CHANNEL_URL})"

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

# ========== استقبال الاسم الجديد للتطبيق (الاسم الظاهر فقط) ==========
@bot.message_handler(func=lambda m: is_admin(m.from_user.id)
                     and isinstance(user_states.get(m.from_user.id, ""), str)
                     and user_states.get(m.from_user.id, "").startswith("waiting_rename_app|"))
def receive_app_new_name(message):
    tg_id = message.from_user.id
    state = user_states[tg_id]
    app_code = state.split("|", 1)[1]

    new_name = message.text.strip() if message.text else ""
    if not new_name:
        bot.reply_to(message, "❌ الاسم غير صالح، أرسل اسماً نصياً.")
        return

    info = get_app_info(app_code)
    if not info:
        bot.reply_to(message, "❌ التطبيق لم يعد موجوداً.")
        user_states.pop(tg_id, None)
        return

    old_name = info[4]
    rename_app_by_code(app_code, new_name)
    log_admin_action(tg_id, "rename_app", f"app_code={app_code}, old='{old_name}', new='{new_name}'")
    user_states.pop(tg_id, None)

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 رجوع للتطبيق", callback_data=f"appv_{app_code}"))
    bot.reply_to(message,
        f"✅ تم تعديل الاسم الظاهر بنجاح:\n"
        f"من: *{old_name}*\n"
        f"إلى: *{new_name}*\n\n"
        f"ملاحظة: اسم الملف الأصلي لم يتغيّر.",
        parse_mode='Markdown', reply_markup=keyboard
    )

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
    # 🔒 خصم ذرّي يمنع التزامن: لو وصل طلبان بنفس اللحظة (دبل-تاب مثلاً)،
    # فقط واحد ينجح فعلياً، والآخر يُرفض بأمان بدل خصم مزدوج أو رصيد سالب.
    if not try_deduct_balance(tg_id, required):
        bot.reply_to(message, "❌ رصيدك غير كافٍ.")
        user_states.pop(tg_id, None)
        return
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
    # 🔒 نفس الحماية الذرّية المستخدمة في طلب الكسر
    if not try_deduct_balance(tg_id, required):
        bot.reply_to(message, "❌ رصيدك غير كافٍ.")
        user_states.pop(tg_id, None)
        return
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

# ========== رفع تجميد مستخدم: استقبال User ID ==========
@bot.message_handler(func=lambda m: is_admin(m.from_user.id)
                     and user_states.get(m.from_user.id) == "waiting_unfreeze_user_id")
def receive_unfreeze_user_id(message):
    tg_id = message.from_user.id
    raw = message.text.strip() if message.text else ""
    if not raw.isdigit():
        bot.reply_to(message, "❌ معرّف غير صالح، أرسل User ID رقمي فقط.")
        return
    target_id = int(raw)
    user = get_user(target_id)
    if not user:
        bot.reply_to(message, "❌ هذا المستخدم غير مسجل في البوت.")
        user_states.pop(tg_id, None)
        return

    is_frozen = user[11] if len(user) > 11 else 0
    frozen_bal = user[9] if len(user) > 9 else 0.0

    if not is_frozen:
        bot.reply_to(message, "ℹ️ هذا المستخدم غير مجمد أصلاً.")
        user_states.pop(tg_id, None)
        return

    # رفع التجميد: إعادة الرصيد المجمد + تصفير العداد
    conn = db_conn()
    c = conn.cursor()
    c.execute("""
        UPDATE users
        SET balance = balance + frozen_balance,
            frozen_balance = 0,
            is_frozen = 0,
            violation_count = 0
        WHERE tg_id=?
    """, (target_id,))
    conn.commit()
    conn.close()

    log_admin_action(tg_id, "unfreeze_user", f"user_id={target_id}, رصيد مُعاد={frozen_bal}")
    user_states.pop(tg_id, None)

    # إشعار المستخدم
    try:
        bot.send_message(
            target_id,
            "✅ تم رفع التجميد عن حسابك من قِبل الإدارة.\n"
            "رصيدك المجمد أُعيد إليك وعداد المخالفات صُفِّر."
        )
    except Exception:
        pass

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 رجوع للوحة الإدارة", callback_data="admin_panel"))
    bot.reply_to(message,
        f"✅ تم رفع التجميد عن المستخدم `{target_id}`\n"
        f"💰 الرصيد المُعاد: {frozen_bal:.2f}$",
        parse_mode='Markdown', reply_markup=keyboard
    )

# ========== تعديل رصيد مستخدم: استقبال User ID ==========
@bot.message_handler(func=lambda m: is_admin(m.from_user.id)
                     and user_states.get(m.from_user.id) == "waiting_balance_user_id")
def receive_balance_target_id(message):
    tg_id = message.from_user.id
    raw = message.text.strip() if message.text else ""
    if not raw.isdigit():
        bot.reply_to(message, "❌ معرّف غير صالح، أرسل User ID رقمي فقط.")
        return
    target_id = int(raw)
    user = get_user(target_id)
    if not user:
        bot.reply_to(message, "❌ هذا المستخدم غير مسجل في البوت.")
        user_states.pop(tg_id, None)
        bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
        return

    user_states.pop(tg_id, None)
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("➕ إضافة رصيد", callback_data=f"baladmin_op|{target_id}|add"))
    keyboard.add(InlineKeyboardButton("➖ خصم رصيد", callback_data=f"baladmin_op|{target_id}|sub"))
    keyboard.add(InlineKeyboardButton("✏️ تعيين قيمة الرصيد", callback_data=f"baladmin_op|{target_id}|set"))
    keyboard.add(InlineKeyboardButton("🔙 إلغاء", callback_data="admin_panel"))
    bot.reply_to(message,
        f"✅ المستخدم موجود.\n"
        f"🆔 `{target_id}` | @{user[1] or 'بدون'}\n"
        f"💰 الرصيد الحالي: {user[2]} دولار\n\n"
        f"اختر العملية:",
        parse_mode='Markdown', reply_markup=keyboard
    )

# ========== تعديل رصيد مستخدم: استقبال القيمة وتنفيذ العملية ==========
@bot.message_handler(func=lambda m: is_admin(m.from_user.id)
                     and isinstance(user_states.get(m.from_user.id, ""), str)
                     and user_states.get(m.from_user.id, "").startswith("waiting_balance_amount|"))
def receive_balance_amount(message):
    tg_id = message.from_user.id
    state = user_states[tg_id]
    _, target_id_str, op = state.split("|")
    target_id = int(target_id_str)

    raw_value = message.text.strip() if message.text else ""
    try:
        amount = float(raw_value)
    except ValueError:
        bot.reply_to(message, "❌ القيمة يجب أن تكون رقماً.")
        return
    if amount < 0:
        bot.reply_to(message, "❌ أدخل قيمة موجبة فقط.")
        return

    # 🔒 إعادة التحقق من وجود المستخدم لحظة التنفيذ (دفاع ثانٍ، في حال حُذف
    # المستخدم أو تغيّرت بياناته بين خطوة اختيار العملية وخطوة إدخال القيمة)
    user = get_user(target_id)
    if not user:
        bot.reply_to(message, "❌ هذا المستخدم غير مسجل في البوت. تم إلغاء العملية.")
        user_states.pop(tg_id, None)
        bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
        return

    if op == "add":
        update_balance(target_id, amount)
        action_desc = f"إضافة {amount}"
    elif op == "sub":
        update_balance(target_id, -amount)
        action_desc = f"خصم {amount}"
    elif op == "set":
        set_balance(target_id, amount)
        action_desc = f"تعيين القيمة إلى {amount}"
    else:
        bot.reply_to(message, "❌ عملية غير معروفة.")
        user_states.pop(tg_id, None)
        return

    new_balance = get_user(target_id)[2]
    log_admin_action(tg_id, "edit_user_balance", f"user_id={target_id}, {action_desc}, new_balance={new_balance}")
    user_states.pop(tg_id, None)

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 رجوع للوحة الإدارة", callback_data="admin_panel"))
    bot.reply_to(message,
        f"✅ تم تنفيذ العملية بنجاح.\n"
        f"🆔 المستخدم: `{target_id}`\n"
        f"العملية: {action_desc}\n"
        f"💰 الرصيد الجديد: {new_balance} دولار",
        parse_mode='Markdown', reply_markup=keyboard
    )


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

def referrals_loop():
    """يشغّل معالج الإحالات كل دقيقتين للمراقبة المستمرة."""
    while True:
        try:
            process_pending_referrals()
        except Exception as e:
            print(f"⚠️ خطأ في referrals_loop: {e}")
        time.sleep(120)  # كل دقيقتين

# ======================= تشغيل البوت =======================
if __name__ == "__main__":
    bot.set_my_commands([telebot.types.BotCommand("start", "بدء البوت")])
    threading.Thread(target=referrals_loop, daemon=True).start()
    threading.Thread(target=auto_backup, daemon=True).start()
    print("✅ البوت يعمل — نظام إحالة جديد (مراقبة 2 ساعة) + نسخ احتياطي كل 30 دقيقة...")

    while True:
        try:
            bot.polling(non_stop=True, timeout=20, long_polling_timeout=15, skip_pending=True)
        except Exception as e:
            print(f"⚠️ خطأ: {e}. إعادة في 10 ثوان...")
            time.sleep(10)