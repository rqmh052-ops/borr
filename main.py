import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
import datetime
import time
import os
import hashlib
import secrets
import shutil
import threading
import requests
import json
import re

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

# ===================== البحث بالذكاء الصناعي (Gemini عبر وسيط) =====================
# رابط API خارجي مجاني لا يتطلب مفتاحاً سرياً، لذا يُترك كثابت عادي (وليس
# متغير بيئة) لتسهيل تعديله مستقبلاً دون الحاجة لإعادة ضبط متغيرات الاستضافة.
AI_SEARCH_API_URL = "https://api.kakarot.cc.cd/api.php"
AI_SEARCH_MODEL = "gemini3.1pro"
# =====================================================

bot = telebot.TeleBot(BOT_TOKEN)
BOT_USERNAME = None   # يُملأ عند أول استدعاء في referrals_loop أو show_balance
user_states = {}
pending_app_messages = {}   # يخزن مؤقتاً رسالة الملف الأصلية من الإدمن
pending_db_restores = {}    # يخزن مؤقتاً مسار ملف .db المرفوع بانتظار تأكيد الاستبدال

# 🔒 يحدّ من عدد طلبات البحث الذكي المتزامنة فعلياً تجاه محرك الذكاء الصناعي
# الخارجي. بدون هذا، لو بحث عشرات المستخدمين بنفس اللحظة قد تُرسل كل
# الطلبات دفعة واحدة فتتسبب بحظر مؤقت (rate limit) من مزوّد الخدمة المجاني،
# فيفشل البحث للجميع. الـ Semaphore يجعل الطلبات الزائدة تنتظر دورها بهدوء
# (بدون تجميد البوت نفسه، لأن كل بحث يعمل أصلاً في Thread منفصل) بدل أن
# تُرفض جميعاً دفعة واحدة. القيمة تُقرأ من الإعدادات (قابلة للتعديل من لوحة
# الإدمن) عبر _ai_search_semaphore() أدناه.
_ai_search_semaphore_lock = threading.Lock()
_ai_search_semaphore_obj = {"value": None, "limit": None}

def _ai_search_semaphore():
    """ينشئ أو يعيد استخدام Semaphore بنفس الحد الحالي من الإعدادات (يُعاد بناؤه فقط لو تغيّر الحد)."""
    limit = int(float(get_setting('ai_search_max_concurrent') or 4))
    with _ai_search_semaphore_lock:
        if _ai_search_semaphore_obj["value"] is None or _ai_search_semaphore_obj["limit"] != limit:
            _ai_search_semaphore_obj["value"] = threading.Semaphore(limit)
            _ai_search_semaphore_obj["limit"] = limit
        return _ai_search_semaphore_obj["value"]

# يخزن مؤقتاً message_id لآخر رسالة "يجب الاشتراك" أُرسلت لكل مستخدم، بحيث لو
# كرّر /start ضغطاً عدة مرات دون الاشتراك، تُحذف الرسالة القديمة ويُرسل تنبيه
# جديد بدل تكديس عدة رسائل مطالبة بالاشتراك في الدردشة.
last_force_sub_prompt = {}

# يخزن مؤقتاً كود رابط الهدية الذي ضغطه المستخدم وما زال بانتظار الاشتراك
# بالقناة لاستلامه، بحيث لو ضغط زر "تأكد الاشتراك" (بدل إعادة /start) يُصرف
# الرابط له تلقائياً بمجرد تأكيد الاشتراك.
pending_gift_codes = {}

# مفتاح سري للتشفير الداخلي - لا يُشارك أبداً
SECRET_KEY = secrets.token_hex(32)

# ======================= نظام التشفير الداخلي =======================
def generate_app_code(message_id: int) -> str:
    raw = f"{SECRET_KEY}:{message_id}:APP"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    num = int(digest[:14], 16) % 9_000_000 + 1_000_000
    return str(num)

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
        ban_reason TEXT,
        banned_at TEXT
    )''')

    # ترقية آمنة لقواعد بيانات قديمة لا تحتوي هذه الأعمدة بعد
    for col, definition in [
        ("is_banned", "INTEGER DEFAULT 0"),
        ("ban_reason", "TEXT"),
        ("banned_at", "TEXT"),
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
        UNIQUE(referrer_id, referred_id)
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
        created_at TEXT,
        deducted_amount REAL DEFAULT 0
    )''')

    # ترقية آمنة لقواعد بيانات قديمة لا تحتوي هذا العمود بعد
    try:
        c.execute("ALTER TABLE user_requests ADD COLUMN deducted_amount REAL DEFAULT 0")
    except Exception:
        pass

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

    # ترقية آمنة لقواعد بيانات قديمة: عمود الكلمات المفتاحية الاختياري
    # المستخدم في البحث بالذكاء الصناعي فقط (لا علاقة له بوصف الرفع الأصلي
    # ولا يُعرض أبداً للمستخدم النهائي بشكل مباشر، يُستخدم فقط كسياق إضافي
    # يُرسَل لمحرك البحث الذكي ليتعرف على التطبيق من وصف حر يكتبه المستخدم).
    try:
        c.execute("ALTER TABLE app_codes ADD COLUMN keywords TEXT")
    except Exception:
        pass

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

    # ===================== روابط الهدايا =====================
    # رابط هدية ينشئه الإدمن: عدد محدد من الاستخدامات + قيمة نقاط ثابتة لكل
    # استخدام. كل ضغطة من مستخدم مختلف تُنقص remaining_uses بواحد حتى تنفد.
    c.execute('''CREATE TABLE IF NOT EXISTS gift_links (
        code TEXT PRIMARY KEY,
        points REAL NOT NULL,
        max_uses INTEGER NOT NULL,
        remaining_uses INTEGER NOT NULL,
        created_by INTEGER,
        created_at TEXT
    )''')

    # يسجّل من استخدم كل رابط هدية لمنع نفس المستخدم من استخدام نفس الرابط
    # أكثر من مرة (تسجيل مزدوج عبر ضغط متكرر أو إعادة فتح الرابط).
    c.execute('''CREATE TABLE IF NOT EXISTS gift_link_redemptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gift_code TEXT NOT NULL,
        tg_id INTEGER NOT NULL,
        redeemed_at TEXT,
        UNIQUE(gift_code, tg_id)
    )''')

    # ===================== الهدية اليومية (Daily Streak) =====================
    # last_claim_date بصيغة YYYY-MM-DD (بتوقيت السيرفر). streak_count يزيد كل
    # يوم متتالٍ ويُصفَّر لو فات يوم بدون استلام. bonus_used_date يمنع صرف
    # بونص الإحالة (50% لمرة واحدة باليوم) أكثر من مرة في نفس اليوم.
    c.execute('''CREATE TABLE IF NOT EXISTS daily_streaks (
        tg_id INTEGER PRIMARY KEY,
        last_claim_date TEXT,
        streak_count INTEGER DEFAULT 0,
        bonus_used_date TEXT
    )''')

    # ===================== تحويل النقاط بين المستخدمين =====================
    c.execute('''CREATE TABLE IF NOT EXISTS transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL,
        receiver_id INTEGER NOT NULL,
        amount_sent REAL NOT NULL,
        amount_received REAL NOT NULL,
        tax_amount REAL NOT NULL,
        created_at TEXT
    )''')

    # ===================== المهام (يضيفها الإدمن يدوياً) =====================
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        reward REAL NOT NULL,
        is_active INTEGER DEFAULT 1,
        created_by INTEGER,
        created_at TEXT
    )''')

    # يسجّل من أنجز كل مهمة لمنع تكرار الاحتساب لنفس المستخدم على نفس المهمة
    c.execute('''CREATE TABLE IF NOT EXISTS task_completions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        tg_id INTEGER NOT NULL,
        completed_at TEXT,
        UNIQUE(task_id, tg_id)
    )''')

    # الإعدادات الافتراضية
    defaults = {
        'referrals_for_feature': '2',
        'referrals_for_vip': '10',
        'maintenance_mode': '0',

        # --- تكلفة الطلبات والـVIP بالنقاط (مستقلة عن نظام الإحالات) ---
        'request_cost_points': '2',  # تكلفة طلب كسر/رفع بالنقاط
        'vip_cost_points': '5',      # تكلفة شراء اشتراك VIP بالنقاط

        # --- الهدية اليومية ---
        'daily_gift_enabled': '1',
        'daily_gift_base': '1',          # قيمة الهدية في اليوم الأول من التتالي
        'daily_gift_increment': '0.5',   # الزيادة لكل يوم متتالٍ إضافي
        'daily_gift_max_streak': '7',    # أقصى يوم تتوقف عنده الزيادة
        'daily_gift_referral_bonus_percent': '50',  # % إضافي إن أحال شخصاً اليوم نفسه (مرة واحدة باليوم)

        # --- تحويل النقاط ---
        'transfer_enabled': '1',
        'transfer_tax_enabled': '1',
        'transfer_tax_percent': '20',    # المستلم يستلم المبلغ ناقص هذه النسبة
        'transfer_min_amount': '1',

        # --- لوحة المتصدرين ---
        'leaderboard_enabled': '1',

        # --- المهام ---
        'tasks_enabled': '1',

        # --- البحث بالذكاء الصناعي ---
        'ai_search_enabled': '1',
        'ai_search_max_concurrent': '4',   # أقصى عدد طلبات متزامنة لمحرك الذكاء الصناعي
        'ai_search_timeout_seconds': '12', # أقصى وقت انتظار لكل طلب قبل اعتباره فاشلاً
        'ai_search_min_chars': '2',        # أدنى عدد أحرف مقبول في استعلام البحث
        'ai_search_max_chars': '80',       # أقصى عدد أحرف مقبول في استعلام البحث
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
    """
    🔧 إصلاح: كانت تستخدم UPDATE فقط، وهو يفشل بصمت (بدون أي خطأ ظاهر) إذا
    لم يكن المفتاح موجوداً أصلاً في settings - فيظهر للإدمن "✅ تم التحديث"
    رغم أن القيمة لم تُحفظ فعلياً. الآن تُدرج المفتاح لو لم يكن موجوداً.
    """
    conn = db_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )
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

    # 🔒 إصلاح خطأ حقيقي: بدون هذا الاستدعاء، رفع نسخة قديمة لا تحتوي أعمدة/
    # جداول أُضيفت لاحقاً (مثل is_banned أو نظام النقاط) كان يتسبب بانهيار
    # صامت في كل تفاعل لاحق يحتاج تلك الأعمدة (no such column). init_db()
    # تستخدم IF NOT EXISTS وALTER TABLE الآمن، فلا تحذف أو تكرر أي بيانات
    # موجودة فعلاً في الملف المستعاد، وفقط تُكمل ما ينقصه.
    init_db()

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

def ban_user(tg_id: int, reason: str = ""):
    """يحظر مستخدماً: يمنعه من استخدام أي ميزة في البوت حتى يُفك حظره."""
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned=1, ban_reason=?, banned_at=? WHERE tg_id=?",
              (reason, datetime.datetime.now().isoformat(), tg_id))
    conn.commit()
    conn.close()

def unban_user(tg_id: int):
    """يفك حظر مستخدم ويعيده لاستخدام البوت بشكل طبيعي."""
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned=0, ban_reason=NULL, banned_at=NULL WHERE tg_id=?", (tg_id,))
    conn.commit()
    conn.close()

def is_user_banned(tg_id: int) -> bool:
    user = get_user(tg_id)
    return bool(user and len(user) > 7 and user[7])

def add_referral(referrer_id, referred_id):
    # 🔒 الإدمن لا يخضع لنظام الإحالات إطلاقاً: لا كمُحيل (يحصل مكافآت)
    # ولا كمُحال (يُحسب لغيره). هذا يمنع أي احتساب أو مخالفة بحق/بسبب الإدمن.
    if is_admin(referrer_id) or is_admin(referred_id):
        return False
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

# ======================= الهدية اليومية (Daily Streak) =======================
def _today_str() -> str:
    return datetime.date.today().isoformat()

def _yesterday_str() -> str:
    return (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

def get_daily_streak_row(tg_id: int):
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT tg_id, last_claim_date, streak_count, bonus_used_date FROM daily_streaks WHERE tg_id=?", (tg_id,))
    row = c.fetchone()
    conn.close()
    return row

def can_claim_daily_gift(tg_id: int) -> bool:
    row = get_daily_streak_row(tg_id)
    if not row:
        return True
    return row[1] != _today_str()

def calculate_daily_gift_amount(tg_id: int):
    """
    يحسب قيمة الهدية القادمة والـ streak الجديد دون أي كتابة على القاعدة.
    يُعيد (amount, new_streak, did_referral_today) بحيث يُستخدم نفس الحساب
    للعرض المسبق (preview) ولحظة الصرف الفعلي.
    """
    base = float(get_setting('daily_gift_base') or 1)
    increment = float(get_setting('daily_gift_increment') or 0.5)
    max_streak = int(float(get_setting('daily_gift_max_streak') or 7))
    bonus_percent = float(get_setting('daily_gift_referral_bonus_percent') or 50)

    row = get_daily_streak_row(tg_id)
    if row and row[1] == _yesterday_str():
        new_streak = min(row[2] + 1, max_streak)
    else:
        new_streak = 1  # أول استلام، أو انقطع التتالي

    amount = base + increment * (new_streak - 1)

    # هل أحال هذا المستخدم شخصاً واستُكملت إحالته اليوم، ولم يصرف بونص اليوم بعد؟
    did_referral_today = False
    bonus_used_date = row[3] if row else None
    if bonus_used_date != _today_str():
        conn = db_conn()
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='completed' AND completed_at LIKE ?",
            (tg_id, f"{_today_str()}%")
        )
        count_today = c.fetchone()[0]
        conn.close()
        if count_today > 0:
            did_referral_today = True
            amount += amount * (bonus_percent / 100)

    return round(amount, 2), new_streak, did_referral_today

def claim_daily_gift(tg_id: int):
    """
    يصرف الهدية اليومية فعلياً: يحسب القيمة، يضيفها للرصيد، ويحدّث التتالي.
    يُعيد (amount, new_streak, did_referral_today) أو None لو سبق الاستلام اليوم.
    """
    if not can_claim_daily_gift(tg_id):
        return None
    amount, new_streak, did_referral_today = calculate_daily_gift_amount(tg_id)

    conn = db_conn()
    c = conn.cursor()
    today = _today_str()
    bonus_date_value = today if did_referral_today else None
    c.execute("""
        INSERT INTO daily_streaks (tg_id, last_claim_date, streak_count, bonus_used_date)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(tg_id) DO UPDATE SET
            last_claim_date = excluded.last_claim_date,
            streak_count = excluded.streak_count,
            bonus_used_date = COALESCE(excluded.bonus_used_date, daily_streaks.bonus_used_date)
    """, (tg_id, today, new_streak, bonus_date_value))
    conn.commit()
    conn.close()

    update_balance(tg_id, amount)
    return amount, new_streak, did_referral_today

# ======================= تحويل النقاط بين المستخدمين =======================
def calculate_transfer(amount: float):
    """يحسب المبلغ الذي سيستلمه الطرف الآخر وقيمة الضريبة المقتطعة، حسب إعدادات الإدمن الحالية."""
    tax_enabled = get_setting('transfer_tax_enabled') == '1'
    tax_percent = float(get_setting('transfer_tax_percent') or 0) if tax_enabled else 0
    tax_amount = round(amount * (tax_percent / 100), 2)
    amount_received = round(amount - tax_amount, 2)
    return amount_received, tax_amount

def execute_transfer(sender_id: int, receiver_id: int, amount: float) -> bool:
    """
    تحويل ذرّي: يخصم من المرسل أولاً (try_deduct_balance يمنع التزامن/الرصيد السالب)،
    ولو فشل الخصم لا يحدث أي تغيير. القيمة المُستلمة بعد الضريبة فقط هي ما يُضاف للمستقبِل.
    """
    if not try_deduct_balance(sender_id, amount):
        return False
    amount_received, tax_amount = calculate_transfer(amount)
    update_balance(receiver_id, amount_received)

    conn = db_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO transfers (sender_id, receiver_id, amount_sent, amount_received, tax_amount, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sender_id, receiver_id, amount, amount_received, tax_amount, datetime.datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    return True

# ======================= لوحة المتصدرين =======================
def get_leaderboard(limit: int = 10):
    """أعلى المستخدمين بعدد الإحالات الناجحة فقط (لا علاقة بالرصيد)."""
    conn = db_conn()
    c = conn.cursor()
    c.execute("""
        SELECT r.referrer_id, COUNT(*) as cnt, u.username
        FROM referrals r
        LEFT JOIN users u ON u.tg_id = r.referrer_id
        WHERE r.status = 'completed'
        GROUP BY r.referrer_id
        ORDER BY cnt DESC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_user_rank(tg_id: int):
    """يُعيد (ترتيب المستخدم, عدد إحالاته الناجحة) حتى لو لم يكن ضمن أعلى القائمة."""
    conn = db_conn()
    c = conn.cursor()
    c.execute("""
        SELECT referrer_id, COUNT(*) as cnt
        FROM referrals
        WHERE status='completed'
        GROUP BY referrer_id
        ORDER BY cnt DESC
    """)
    rows = c.fetchall()
    conn.close()
    for idx, (rid, cnt) in enumerate(rows, start=1):
        if rid == tg_id:
            return idx, cnt
    return None, 0

# ======================= المهام (يضيفها الإدمن يدوياً) =======================
def create_task(title: str, description: str, reward: float, created_by: int) -> int:
    conn = db_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO tasks (title, description, reward, is_active, created_by, created_at) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (title, description, reward, created_by, datetime.datetime.now().isoformat())
    )
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id

def get_active_tasks_for_user(tg_id: int):
    """المهام النشطة التي لم يُنجزها هذا المستخدم بعد."""
    conn = db_conn()
    c = conn.cursor()
    c.execute("""
        SELECT t.id, t.title, t.description, t.reward
        FROM tasks t
        WHERE t.is_active = 1
        AND t.id NOT IN (SELECT task_id FROM task_completions WHERE tg_id = ?)
        ORDER BY t.id DESC
    """, (tg_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_tasks():
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT id, title, reward, is_active FROM tasks ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def get_task(task_id: int):
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT id, title, description, reward, is_active FROM tasks WHERE id=?", (task_id,))
    row = c.fetchone()
    conn.close()
    return row

def toggle_task_active(task_id: int):
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE tasks SET is_active = 1 - is_active WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

def delete_task(task_id: int):
    conn = db_conn()
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    c.execute("DELETE FROM task_completions WHERE task_id=?", (task_id,))
    conn.commit()
    conn.close()

def complete_task_for_user(task_id: int, tg_id: int) -> float | None:
    """
    يسجّل إنجاز المهمة ويمنح المكافأة. يُعيد قيمة المكافأة، أو None لو كانت
    المهمة غير موجودة/غير نشطة أو سبق للمستخدم إنجازها (UNIQUE constraint).
    """
    task = get_task(task_id)
    if not task or not task[4]:  # غير موجودة أو غير نشطة
        return None
    reward = task[3]
    conn = db_conn()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO task_completions (task_id, tg_id, completed_at) VALUES (?, ?, ?)",
            (task_id, tg_id, datetime.datetime.now().isoformat())
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return None  # سبق إنجازها من قبل
    conn.close()
    update_balance(tg_id, reward)
    return reward

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

# ======================= روابط الهدايا =======================
def create_gift_link(points: float, max_uses: int, created_by: int) -> str:
    """
    ينشئ رابط هدية جديد بكود عشوائي 8 أحرف/أرقام، بعدد استخدامات وقيمة نقاط
    محددة من الإدمن. يُعيد الكود المُنشأ.
    """
    conn = db_conn()
    c = conn.cursor()
    for _ in range(50):
        code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        try:
            c.execute(
                "INSERT INTO gift_links (code, points, max_uses, remaining_uses, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (code, points, max_uses, max_uses, created_by, datetime.datetime.now().isoformat())
            )
            conn.commit()
            conn.close()
            return code
        except sqlite3.IntegrityError:
            continue
    conn.close()
    raise RuntimeError("تعذّر إنشاء كود رابط هدية فريد بعد عدة محاولات.")

def get_gift_link(code: str):
    """يعيد (code, points, max_uses, remaining_uses, created_by, created_at) أو None."""
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT code, points, max_uses, remaining_uses, created_by, created_at FROM gift_links WHERE code=?", (code,))
    row = c.fetchone()
    conn.close()
    return row

def redeem_gift_link(code: str, tg_id: int) -> str:
    """
    يحاول صرف رابط الهدية لمستخدم معيّن بشكل ذرّي (يمنع التزامن/الاستخدام
    المزدوج). يُعيد إحدى القيم:
    'ok'        → تم الصرف بنجاح والنقاط أُضيفت
    'not_found' → الرابط غير موجود
    'exhausted' → الرابط نفد (remaining_uses = 0)
    'already'   → هذا المستخدم استخدم هذا الرابط من قبل
    """
    conn = db_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT points, remaining_uses FROM gift_links WHERE code=?", (code,))
        row = c.fetchone()
        if not row:
            conn.close()
            return "not_found"
        points, remaining = row
        if remaining <= 0:
            conn.close()
            return "exhausted"

        # تسجيل الاستخدام أولاً (UNIQUE يمنع التكرار لنفس المستخدم ذرّياً)
        try:
            c.execute(
                "INSERT INTO gift_link_redemptions (gift_code, tg_id, redeemed_at) VALUES (?, ?, ?)",
                (code, tg_id, datetime.datetime.now().isoformat())
            )
        except sqlite3.IntegrityError:
            conn.close()
            return "already"

        # خصم ذرّي من العداد، يضمن عدم نزوله تحت الصفر حتى مع تزامن طلبات
        c.execute(
            "UPDATE gift_links SET remaining_uses = remaining_uses - 1 WHERE code=? AND remaining_uses > 0",
            (code,)
        )
        if c.rowcount == 0:
            # نفد العداد بين القراءة والكتابة (سباق نادر) - نتراجع عن تسجيل الاستخدام
            c.execute("DELETE FROM gift_link_redemptions WHERE gift_code=? AND tg_id=?", (code, tg_id))
            conn.commit()
            conn.close()
            return "exhausted"

        conn.commit()
        conn.close()
        update_balance(tg_id, points)
        return "ok"
    except Exception:
        conn.close()
        raise

REFERRAL_REWARD  = 0.5       # مكافأة الإحالة الفورية (تُحتسب نهائياً فور الاشتراك في القناة)

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


_referrals_lock = threading.Lock()

def process_pending_referrals():
    """
    يعالج الإحالات المعلّقة بمنطق مبسّط بدون أي مراقبة لاحقة:
    ─ الإحالة pending تبقى pending إلى أن يشترك المُحال في القناة.
    ─ بمجرد التحقق من الاشتراك → الإحالة تُحتسب فوراً ونهائياً (completed)
      وتُمنح المكافأة مباشرة. لا توجد أي نافذة مراقبة ولا أي خصم لاحق.
    """
    if not _referrals_lock.acquire(blocking=False):
        return
    try:
        conn = db_conn()
        c = conn.cursor()
        now = datetime.datetime.now()

        # جلب كل الإحالات المعلقة (لم تُحتسب بعد)
        c.execute("""
            SELECT id, referrer_id, referred_id
            FROM referrals
            WHERE status = 'pending'
        """)
        rows = c.fetchall()

        for ref_id, referrer_id, referred_id in rows:
            if not check_subscription(referred_id):
                continue

            # اشترك → احتساب نهائي فوري، بلا مراقبة لاحقة
            c.execute("""
                UPDATE referrals
                SET status='completed', completed_at=?
                WHERE id=?
            """, (now.isoformat(), ref_id))
            conn.commit()
            update_balance(referrer_id, REFERRAL_REWARD)
            referred_username = _get_username(referred_id)
            ref_link = _user_link(referred_id, referred_username)
            try:
                bot.send_message(
                    referrer_id,
                    f"✅ تم احتساب إحالة جديدة بنجاح!\n"
                    f"👤 {ref_link}\n"
                    f"💰 +{REFERRAL_REWARD} نقطة ⭐",
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

    🔒 إصلاح خطأ حرج: النسخة السابقة كانت تحاول الإدراج مرة واحدة، وإن تعارض
    الكود (نادر جداً، ~1 من 9 ملايين، لكن وارد فعلياً) كانت تجرّب app_code+1
    عبر INSERT OR IGNORE مرة واحدة فقط دون التحقق من نجاحها. لو تصادم
    الاحتمالان معاً (تصادم مضاعف، أندر لكن ممكن رياضياً)، كانت الدالة تُعيد
    app_code "بثقة" رغم أن لا شيء أُدرج فعلياً في القاعدة - فيظهر للإدمن
    "✅ تم النشر بنجاح" بينما التطبيق غير موجود فعلياً ولا يمكن لأي مستخدم
    الوصول إليه أبداً (فقدان بيانات صامت تماماً).
    الآن: حلقة إعادة محاولة حقيقية (حتى 10 محاولات بكود مختلف في كل مرة)،
    مع التحقق الصريح من نجاح كل محاولة عبر conn.commit() الناجح فقط داخل
    try ناجح فعلياً (لا INSERT OR IGNORE صامت). لو فشلت كل المحاولات (شبه
    مستحيل عملياً)، تُرفع RuntimeError واضحة بدل إرجاع كود وهمي - المستدعي
    (receive_app_caption_and_publish) يمسكها أصلاً عبر except Exception
    العام الموجود ويُبلغ الإدمن بالفشل الحقيقي بدل رسالة نجاح كاذبة.
    """
    conn = db_conn()
    c = conn.cursor()
    try:
        # تأكد أولاً من عدم وجود تسجيل سابق لنفس الرسالة (نشر مكرر بالخطأ)
        c.execute("SELECT app_code FROM app_codes WHERE channel_msg_id=?", (channel_msg_id,))
        existing = c.fetchone()
        if existing:
            return existing[0]

        app_code = generate_app_code(channel_msg_id)
        for attempt in range(10):
            try:
                c.execute(
                    "INSERT INTO app_codes (app_code, channel_msg_id, category, is_vip, name, added_date) VALUES (?, ?, ?, ?, ?, ?)",
                    (app_code, channel_msg_id, category, 1 if is_vip else 0, name, datetime.datetime.now().isoformat())
                )
                conn.commit()
                return app_code  # نصل هنا فقط لو نجح الإدراج فعلاً (commit بدون استثناء)
            except sqlite3.IntegrityError:
                conn.rollback()
                # تعارض على app_code أو channel_msg_id - نولّد كوداً مختلفاً ونعيد المحاولة
                if attempt == 0:
                    # احتمال نادر إضافي: قد يكون channel_msg_id نفسه هو سبب
                    # التعارض (سباق نادر بين طلبين متزامنين لنفس الرسالة) -
                    # نتحقق من ذلك قبل افتراض أن المشكلة في app_code فقط.
                    c.execute("SELECT app_code FROM app_codes WHERE channel_msg_id=?", (channel_msg_id,))
                    existing2 = c.fetchone()
                    if existing2:
                        return existing2[0]
                app_code = str((int(app_code) + 1 - 1_000_000) % 9_000_000 + 1_000_000)

        # فشلت كل المحاولات العشر (احتمال شبه معدوم رياضياً) - نرفض بصراحة
        # بدل إرجاع كود غير محفوظ فعلياً في القاعدة.
        raise RuntimeError(
            f"تعذّر توليد app_code فريد بعد 10 محاولات لـ channel_msg_id={channel_msg_id}. "
            f"لم يُنشر أي شيء في قاعدة البيانات."
        )
    finally:
        conn.close()

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

def get_app_keywords(app_code: str):
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT keywords FROM app_codes WHERE app_code=?", (app_code,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_app_keywords(app_code: str, keywords: str):
    """
    يحفظ الكلمات المفتاحية/الوصف الاختياري الذي يراه محرك البحث بالذكاء
    الصناعي فقط. لا علاقة له بوصف الرفع الأصلي ولا يُعرض للمستخدمين أبداً.
    """
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE app_codes SET keywords=? WHERE app_code=?", (keywords, app_code))
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

# ======================= البحث بالذكاء الصناعي =======================
def _ask_gemini(prompt: str, timeout: int) -> str | None:
    """
    استدعاء خام لمحرك الذكاء الصناعي الخارجي (وسيط Gemini مجاني عبر GET).
    يُعيد فقط نص حقل "answer"، أو None لو فشل الطلب أو كان الرد غير صالح.
    لا يُلقي أي استثناء للخارج عمداً - كل فشل يُعامل بصمت ليقرر المتصل
    إعادة المحاولة أو الاستسلام.
    """
    try:
        resp = requests.get(
            AI_SEARCH_API_URL,
            params={"model": AI_SEARCH_MODEL, "question": prompt},
            timeout=timeout
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("success"):
            return None
        answer = data.get("answer")
        if not answer or not isinstance(answer, str):
            return None
        return answer.strip()
    except Exception:
        return None

def _build_ai_search_prompt(query: str, candidates: list) -> str:
    """
    يبني الـ prompt المُرسَل لمحرك الذكاء الصناعي. القائمة المُرسَلة هنا هي
    *فقط* (رقم مرجعي تسلسلي محلي، اسم العرض، كلمات مفتاحية اختيارية) لكل
    تطبيق غير VIP - لا يُرسَل أبداً app_code الحقيقي، ولا channel_msg_id،
    ولا وصف وقت الرفع، ولا أي بيانات أخرى. الرقم المرجعي هو فهرس محلي مؤقت
    (index في القائمة) يُترجَم لاحقاً إلى app_code الحقيقي داخلياً في الكود
    فقط - الموديل لا يرى ولا يستطيع التأثير على app_code الفعلي إطلاقاً.
    """
    lines = []
    for idx, name, keywords in candidates:
        entry = f"{idx}) {name}"
        if keywords:
            entry += f" — كلمات مفتاحية: {keywords}"
        lines.append(entry)
    catalog = "\n".join(lines)

    return (
        "أنت محرك بحث داخلي صامت، لست مساعداً محادثاً. "
        "لديك القائمة التالية من التطبيقات، كل سطر يبدأ برقم مرجعي:\n\n"
        f"{catalog}\n\n"
        f"طلب المستخدم: \"{query}\"\n\n"
        "مهمتك: حدد الرقم المرجعي للتطبيق الوحيد الأقرب لطلب المستخدم بناءً "
        "على الاسم أو الكلمات المفتاحية أعلاه فقط. "
        "تعليمات صارمة يجب اتباعها حرفياً:\n"
        "- ردّك يجب أن يكون الرقم المرجعي فقط، أرقام فقط، بدون أي حرف أو كلمة أو علامة ترقيم أو شرح.\n"
        "- لا تكتب جملاً مثل 'الرقم هو' أو 'وجدت' أو 'خذ هذا الرقم'، اكتب الرقم بمفرده تماماً ولا شيء غيره.\n"
        "- إن لم يوجد أي تطابق منطقي مقبول، اكتب فقط: 0\n"
        "- لا تخترع رقماً غير موجود في القائمة أعلاه."
    )

def _parse_ai_search_reply(reply: str, max_index: int):
    """يستخرج رقماً صحيحاً صالحاً من رد الموديل، متسامح مع أي نص زائد غير منضبط احتياطاً."""
    if reply is None:
        return None
    match = re.search(r'\d+', reply)
    if not match:
        return None
    idx = int(match.group())
    if idx == 0 or idx > max_index:
        return None
    return idx

def ai_search_app(query: str):
    """
    البحث الكامل بالذكاء الصناعي: يبني القائمة المصغّرة (غير VIP فقط)،
    يستدعي المحرك الخارجي، ويُعيد app_code الحقيقي المطابق أو None.
    يحاول مرتين بصمت تام قبل الاستسلام (المستخدم لا يرى أي فرق بين
    المحاولتين، فقط ينتظر قليلاً أطول عند الحاجة للمحاولة الثانية).
    """
    if get_setting('ai_search_enabled') != '1':
        return None

    conn = db_conn()
    c = conn.cursor()
    # 🔒 فصل صارم: استبعاد كل تطبيق VIP من المصدر نفسه، فلا يستطيع محرك
    # الذكاء الصناعي إرجاعه أبداً مهما كان طلب المستخدم - ليس فلتراً بعدياً
    # بل غياب تام عن البيانات المُرسَلة له من الأساس.
    c.execute("SELECT app_code, name, keywords FROM app_codes WHERE is_vip=0")
    rows = c.fetchall()
    conn.close()

    if not rows:
        return None

    # فهرس محلي مؤقت (1..n) يُستخدم فقط داخل هذا الاستدعاء لترجمة رد
    # الموديل إلى app_code الحقيقي، ولا يُخزَّن ولا يُستخدم في أي مكان آخر
    code_by_index = {}
    candidates = []
    for i, (app_code, name, keywords) in enumerate(rows, start=1):
        code_by_index[i] = app_code
        candidates.append((i, name, keywords))

    prompt = _build_ai_search_prompt(query, candidates)
    timeout = int(float(get_setting('ai_search_timeout_seconds') or 12))
    semaphore = _ai_search_semaphore()

    for attempt in range(2):
        acquired = semaphore.acquire(timeout=timeout + 5)
        if not acquired:
            continue
        try:
            reply = _ask_gemini(prompt, timeout)
        finally:
            semaphore.release()

        idx = _parse_ai_search_reply(reply, len(candidates))
        if idx is not None:
            return code_by_index.get(idx)
        # فشلت المحاولة (رد فارغ/غامض/فشل شبكة) → إعادة محاولة صامتة واحدة فقط

    return None


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
        # نسخ الرسالة من قناة قاعدة البيانات للمستخدم بدون إظهار "محوّل من".
        # 🔒 protect_content=True: يمنع المستخدم من تحويل (Forward) أو حفظ
        # الملف خارج البوت بالكامل.
        bot.copy_message(
            chat_id=tg_id,
            from_chat_id=DB_CHANNEL_ID,
            message_id=channel_msg_id,
            protect_content=True
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
    ]
    keyboard.add(*buttons[:2])
    keyboard.add(buttons[2])
    keyboard.add(buttons[3])
    if get_setting('ai_search_enabled') == '1':
        keyboard.add(InlineKeyboardButton("🤖 بحث بالذكاء الصناعي", callback_data="ai_search_start"))

    extra_row = []
    if get_setting('daily_gift_enabled') == '1':
        extra_row.append(InlineKeyboardButton("🎁 الهدية اليومية", callback_data="daily_gift"))
    if get_setting('transfer_enabled') == '1':
        extra_row.append(InlineKeyboardButton("💸 تحويل نقاط", callback_data="transfer_start"))
    if extra_row:
        keyboard.add(*extra_row)

    extra_row2 = []
    if get_setting('tasks_enabled') == '1':
        extra_row2.append(InlineKeyboardButton("📋 المهام", callback_data="show_tasks"))
    if get_setting('leaderboard_enabled') == '1':
        extra_row2.append(InlineKeyboardButton("🏆 المتصدرين", callback_data="show_leaderboard"))
    if extra_row2:
        keyboard.add(*extra_row2)

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
        InlineKeyboardButton("🚫 حظر / فك حظر مستخدم", callback_data="admin_ban_user"),
        InlineKeyboardButton("🎁 إنشاء رابط هدية", callback_data="create_gift_link"),
        InlineKeyboardButton("📋 إدارة المهام", callback_data="admin_tasks"),
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

        # 🚫 المستخدم المحظور: يُمنع من أي تفاعل مع البوت (الإدمن مستثنى دوماً)
        if not is_admin(tg_id) and is_user_banned(tg_id):
            bot.answer_callback_query(call.id, "🚫 تم حظرك من استخدام البوت.", show_alert=True)
            return

        # التحقق من الاشتراك
        if data not in ['check_subscription', 'dummy']:
            if not check_subscription(tg_id):
                bot.edit_message_text("⚠️ يجب الاشتراك في القناة أولاً لاستخدام البوت:",
                                      chat_id, message_id, reply_markup=force_sub_keyboard())
                return

        # ========== القائمة الرئيسية ==========
        if data == "main_menu":
            user_states.pop(tg_id, None)
            bot.edit_message_text("🏠 اختر من القائمة:", chat_id, message_id, reply_markup=main_menu_keyboard(tg_id))

        elif data == "check_subscription":
            if check_subscription(tg_id):
                last_force_sub_prompt.pop(tg_id, None)
                # تشغيل فوري لمعالجة الإحالة إن وُجدت
                threading.Thread(target=process_pending_referrals, daemon=True).start()
                bot.edit_message_text("✅ تم التأكيد!", chat_id, message_id)
                bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
                # صرف رابط الهدية المعلّق (إن وُجد) بعد تأكيد الاشتراك فعلياً
                pending_gift = pending_gift_codes.pop(tg_id, None)
                if pending_gift:
                    _try_redeem_gift(tg_id, pending_gift)
            else:
                bot.answer_callback_query(call.id, "❌ لم تشترك بعد.", show_alert=True)

        elif data == "ai_search_start":
            if get_setting('ai_search_enabled') != '1':
                bot.answer_callback_query(call.id, "❌ البحث بالذكاء الصناعي معطّل حالياً.", show_alert=True)
                return
            user_states[tg_id] = "waiting_ai_search_query"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("❌ إلغاء", callback_data="main_menu"))
            bot.edit_message_text(
                "🤖 اكتب وصف التطبيق اللي تدور عليه بأي صيغة تحبها:",
                chat_id, message_id, reply_markup=keyboard
            )

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
                keyboard.add(InlineKeyboardButton("🔑 كلمات مفتاحية للبحث الذكي", callback_data=f"setkeywords_{app_code}"))

            # 🔧 إصلاح: زر الرجوع يجب أن يعيدك لنفس القائمة التي جئت منها.
            # تطبيق VIP له أيضاً category عادي (مثل "ألعاب")، فلو رجّعناه دائماً
            # لـ catv_ (قائمة التصنيف المجانية) فستظهر له تطبيقات مجانية بدل
            # قائمة VIP التي فتح منها التطبيق أصلاً - وهذا كان سبب المشكلة.
            if is_vip:
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_vip"))
            else:
                categories = get_categories()
                cat_id_back = next((cid for cid, cname in categories if cname == category), None)
                if cat_id_back:
                    keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data=f"catv_{cat_id_back}_p_1"))
                else:
                    keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_categories"))

            label = "👑 VIP" if is_vip else "📱"
            safe_name = escape_markdown(name)
            bot.edit_message_text(
                f"{label} *{safe_name}*\n⬇️ {downloads} تحميل",
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
            safe_current_name = escape_markdown(current_name)
            user_states[tg_id] = f"waiting_rename_app|{app_code}"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("❌ إلغاء", callback_data=f"appv_{app_code}"))
            bot.edit_message_text(
                f"✏️ الاسم الحالي: *{safe_current_name}*\n\n"
                f"أرسل الاسم الجديد الذي سيظهر في نتائج البحث والقائمة فقط\n"
                f"(اسم الملف الأصلي لن يتغيّر):",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        # ========== تعديل الكلمات المفتاحية (للبحث بالذكاء الصناعي فقط) ==========
        elif data.startswith("setkeywords_"):
            if not is_admin(tg_id):
                return
            app_code = data[len("setkeywords_"):]
            info = get_app_info(app_code)
            if not info:
                bot.answer_callback_query(call.id, "التطبيق غير موجود.")
                return
            current_keywords = get_app_keywords(app_code)
            safe_current = escape_markdown(current_keywords) if current_keywords else "لا توجد"
            user_states[tg_id] = f"waiting_app_keywords|{app_code}"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("❌ إلغاء", callback_data=f"appv_{app_code}"))
            bot.edit_message_text(
                f"🔑 الكلمات المفتاحية الحالية: *{safe_current}*\n\n"
                f"أرسل الآن وصفاً قصيراً أو كلمات مفتاحية تساعد الذكاء الصناعي على "
                f"فهم هذا التطبيق عند بحث المستخدمين (مثال: \"نتفليكس، أفلام، مسلسلات، بث\").\n"
                f"هذا النص لا يظهر للمستخدمين أبداً، يُستخدم فقط داخلياً لمحرك البحث الذكي.",
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
            if not is_admin(tg_id) and (not user or not user[3]):
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("🔓 شراء VIP", callback_data="buy_vip"))
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
                bot.edit_message_text("👑 *VIP*\nفي الـ VIP تطبيقات أقوى بكثير من التطبيقات المجانية.",
                                      chat_id, message_id,
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
            required = float(get_setting('vip_cost_points') or 5)
            # 🔒 خصم ذرّي يمنع التزامن: لو ضغط المستخدم الزر مرتين بسرعة، لا يُخصم منه مرتين
            if try_deduct_balance(tg_id, required):
                set_vip(tg_id)
                bot.edit_message_text("🎉 أصبحت VIP!", chat_id, message_id)
                bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
            else:
                user = get_user(tg_id)
                balance = user[2] if user else 0
                remaining = max(required - balance, 0)
                bot.answer_callback_query(
                    call.id,
                    f"رصيدك غير كافٍ. تحتاج {required} نقطة ⭐، رصيدك الحالي {balance:.2f}، ينقصك {remaining:.2f}.",
                    show_alert=True
                )

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

            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='completed'", (tg_id,))
            refs = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='pending'", (tg_id,))
            pending_count = c.fetchone()[0]
            conn.close()

            ref_code = get_referral_code(tg_id)
            bu = BOT_USERNAME or bot.get_me().username
            ref_link = f"https://t.me/{bu}?start=ref_{ref_code}"

            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(InlineKeyboardButton("📤 مشاركة البوت", switch_inline_query=ref_link))
            if pending_count > 0:
                keyboard.add(InlineKeyboardButton(
                    f"🔵 الاشتراكات المعلقة ({pending_count})",
                    callback_data="show_pending_referrals"
                ))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))

            bot.edit_message_text(
                f"💰 *الرصيد الحالي:* {balance:.2f} نقطة ⭐"
                f"\n📊 *الإحالات الناجحة:* {refs}"
                f"\n⏳ *معلقة:* {pending_count}\n\n"
                f"كل إحالة = {REFERRAL_REWARD} نقطة ⭐\n"
                f"• {get_setting('request_cost_points')} نقطة ⭐ = طلب كسر/رفع\n"
                f"• {get_setting('vip_cost_points')} نقطة ⭐ = VIP\n\n"
                f"➖➖➖➖➖➖➖➖➖➖\n"
                f"🔗 *كود إحالتك:* `{ref_code}`\n"
                f"رابط دعوتك:\n`{ref_link}`",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        # ========== طلب كسر / رفع ==========
        elif data == "make_request":
            required = float(get_setting('request_cost_points') or 2)
            user = get_user(tg_id)
            if not user or user[2] < required:
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("🔗 جلب إحالات", callback_data="show_balance"))
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
                bot.edit_message_text(f"❌ رصيدك غير كافٍ (تحتاج {required} نقطة ⭐).",
                                      chat_id, message_id, reply_markup=keyboard)
                return
            keyboard = InlineKeyboardMarkup(row_width=2)
            keyboard.add(InlineKeyboardButton("🔨 طلب كسر", callback_data="request_crack"))
            keyboard.add(InlineKeyboardButton("📤 رفع تطبيق", callback_data="request_upload"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text("📨 اختر نوع الطلب:", chat_id, message_id, reply_markup=keyboard)

        elif data == "request_crack":
            required = float(get_setting('request_cost_points') or 2)
            user = get_user(tg_id)
            if not user or user[2] < required:
                bot.answer_callback_query(call.id, f"❌ رصيدك غير كافٍ (تحتاج {required} نقطة ⭐).", show_alert=True)
                return
            user_states[tg_id] = "waiting_crack_apk_file"
            keyboard_c = InlineKeyboardMarkup()
            keyboard_c.add(InlineKeyboardButton("❌ إلغاء", callback_data="main_menu"))
            bot.edit_message_text(
                "📎 *طلب كسر تطبيق*\n\n"
                "أرسل ملف التطبيق مباشرةً (.apk أو .apks فقط).\n"
                "⚠️ ملفات من أي نوع آخر لن تُقبل.",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard_c
            )

        elif data == "request_upload":
            user_states[tg_id] = "waiting_upload_file"
            bot.edit_message_text("📤 أرسل ملف التطبيق:", chat_id, message_id)

        # ========== الهدية اليومية ==========
        elif data == "daily_gift":
            if get_setting('daily_gift_enabled') != '1':
                bot.answer_callback_query(call.id, "❌ الهدية اليومية معطّلة حالياً.", show_alert=True)
                return
            result = claim_daily_gift(tg_id)
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            if result is None:
                bot.edit_message_text(
                    "⏳ لقد استلمت هديتك اليوم بالفعل، عُد غداً!",
                    chat_id, message_id, reply_markup=keyboard
                )
                return
            amount, new_streak, did_referral_today = result
            bonus_line = "\n🎉 بونص إحالة اليوم مُضاف!" if did_referral_today else ""
            bot.edit_message_text(
                f"🎁 *تم استلام هديتك اليومية!*\n\n"
                f"💰 المبلغ: {amount} نقطة ⭐\n"
                f"🔥 أيام متتالية: {new_streak}"
                f"{bonus_line}\n\n"
                f"عُد غداً لتزيد التتالي وتحصل على مبلغ أكبر!",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        # ========== تحويل النقاط بين المستخدمين ==========
        elif data == "transfer_start":
            if get_setting('transfer_enabled') != '1':
                bot.answer_callback_query(call.id, "❌ التحويل معطّل حالياً.", show_alert=True)
                return
            user_states[tg_id] = "waiting_transfer_target_id"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("❌ إلغاء", callback_data="main_menu"))
            bot.edit_message_text(
                "💸 *تحويل نقاط*\n\nأرسل الآن User ID الخاص بالمستخدم الذي تريد التحويل له:",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        elif data == "transfer_confirm":
            pending = user_states.get(tg_id)
            if not isinstance(pending, str) or not pending.startswith("confirm_transfer|"):
                bot.answer_callback_query(call.id, "❌ انتهت صلاحية العملية.", show_alert=True)
                return
            _, target_id_str, amount_str = pending.split("|")
            target_id = int(target_id_str)
            amount = float(amount_str)
            user_states.pop(tg_id, None)

            success = execute_transfer(tg_id, target_id, amount)
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            if not success:
                bot.edit_message_text("❌ فشل التحويل، تأكد من رصيدك.", chat_id, message_id, reply_markup=keyboard)
                return
            received, tax = calculate_transfer(amount)
            bot.edit_message_text(
                f"✅ *تم التحويل بنجاح*\n\n"
                f"📤 أرسلت: {amount} نقطة ⭐\n"
                f"💸 الضريبة: {tax} نقطة ⭐\n"
                f"📥 استلم المستخدم: {received} نقطة ⭐",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )
            try:
                bot.send_message(target_id, f"💸 وصلتك حوالة بقيمة {received} نقطة ⭐ من مستخدم آخر!")
            except Exception:
                pass

        elif data == "transfer_cancel":
            user_states.pop(tg_id, None)
            bot.edit_message_text("❌ تم إلغاء عملية التحويل.", chat_id, message_id,
                                  reply_markup=main_menu_keyboard(tg_id))

        # ========== لوحة المتصدرين ==========
        elif data == "show_leaderboard":
            if get_setting('leaderboard_enabled') != '1':
                bot.answer_callback_query(call.id, "❌ لوحة المتصدرين معطّلة حالياً.", show_alert=True)
                return
            top = get_leaderboard(10)
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            if not top:
                lines.append("لا يوجد أحد بعد، كن أول من يحيل!")
            for idx, (rid, cnt, uname) in enumerate(top):
                prefix = medals[idx] if idx < 3 else f"{idx + 1}."
                label = f"@{escape_markdown(uname)}" if uname else f"User {rid}"
                lines.append(f"{prefix} {label} — {cnt} إحالة")

            rank, my_count = get_user_rank(tg_id)
            footer = f"\n\n📍 ترتيبك: #{rank} ({my_count} إحالة)" if rank else "\n\n📍 لم تُحقق أي إحالة بعد."

            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text(
                "🏆 *لوحة المتصدرين (الإحالات)*\n\n" + "\n".join(lines) + footer,
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        # ========== المهام ==========
        elif data == "show_tasks":
            if get_setting('tasks_enabled') != '1':
                bot.answer_callback_query(call.id, "❌ المهام معطّلة حالياً.", show_alert=True)
                return
            tasks = get_active_tasks_for_user(tg_id)
            keyboard = InlineKeyboardMarkup(row_width=1)
            if not tasks:
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
                bot.edit_message_text("📋 لا توجد مهام متاحة لك حالياً، تابع لاحقاً!",
                                      chat_id, message_id, reply_markup=keyboard)
                return
            for t_id, title, desc, reward in tasks:
                keyboard.add(InlineKeyboardButton(f"📌 {title} (+{reward} نقطة ⭐)", callback_data=f"view_task|{t_id}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
            bot.edit_message_text("📋 *المهام المتاحة:*\nاختر مهمة لعرض تفاصيلها:",
                                  chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)

        elif data.startswith("view_task|"):
            t_id = int(data.split("|", 1)[1])
            task = get_task(t_id)
            if not task or not task[4]:
                bot.answer_callback_query(call.id, "❌ هذه المهمة لم تعد متاحة.", show_alert=True)
                return
            _, title, desc, reward, _ = task
            safe_title = escape_markdown(title)
            safe_desc = escape_markdown(desc) if desc else None
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(InlineKeyboardButton("✅ تم الإنجاز، استلم المكافأة", callback_data=f"complete_task|{t_id}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="show_tasks"))
            bot.edit_message_text(
                f"📌 *{safe_title}*\n\n{safe_desc or 'بدون وصف'}\n\n💰 المكافأة: {reward} نقطة ⭐\n\n"
                f"اضغط الزر أدناه بعد إتمام المهمة فعلياً لاستلام المكافأة.",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        elif data.startswith("complete_task|"):
            t_id = int(data.split("|", 1)[1])
            reward = complete_task_for_user(t_id, tg_id)
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("📋 مهام أخرى", callback_data="show_tasks"))
            keyboard.add(InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu"))
            if reward is None:
                bot.edit_message_text("❌ تعذّر احتساب هذه المهمة (ربما أنجزتها من قبل أو لم تعد متاحة).",
                                      chat_id, message_id, reply_markup=keyboard)
                return
            bot.edit_message_text(f"✅ تم منحك {reward} نقطة ⭐ على إنجاز المهمة!",
                                  chat_id, message_id, reply_markup=keyboard)

        # ========== إدارة المهام (إدمن) ==========
        elif data == "admin_tasks":
            if not is_admin(tg_id):
                return
            tasks = get_all_tasks()
            keyboard = InlineKeyboardMarkup(row_width=1)
            for t_id, title, reward, active in tasks:
                status = "🟢" if active else "🔴"
                keyboard.add(InlineKeyboardButton(f"{status} {title} ({reward} نقطة ⭐)", callback_data=f"admin_task_view|{t_id}"))
            keyboard.add(InlineKeyboardButton("➕ إضافة مهمة جديدة", callback_data="admin_add_task"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel"))
            bot.edit_message_text("📋 *إدارة المهام*\n\nاضغط على مهمة للتعديل، أو أضف مهمة جديدة:",
                                  chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)

        elif data == "admin_add_task":
            if not is_admin(tg_id):
                return
            user_states[tg_id] = "waiting_task_title"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("❌ إلغاء", callback_data="admin_tasks"))
            bot.edit_message_text("✏️ أرسل عنوان المهمة (مثال: انضم لقناة X):",
                                  chat_id, message_id, reply_markup=keyboard)

        elif data.startswith("admin_task_view|"):
            if not is_admin(tg_id):
                return
            t_id = int(data.split("|", 1)[1])
            task = get_task(t_id)
            if not task:
                bot.answer_callback_query(call.id, "❌ غير موجودة.", show_alert=True)
                return
            _, title, desc, reward, active = task
            safe_title = escape_markdown(title)
            safe_desc = escape_markdown(desc) if desc else None
            conn = db_conn()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM task_completions WHERE task_id=?", (t_id,))
            completions = c.fetchone()[0]
            conn.close()
            status_label = "🟢 نشطة" if active else "🔴 متوقفة"
            keyboard = InlineKeyboardMarkup(row_width=1)
            toggle_label = "⏸️ إيقاف المهمة" if active else "▶️ تفعيل المهمة"
            keyboard.add(InlineKeyboardButton(toggle_label, callback_data=f"admin_task_toggle|{t_id}"))
            keyboard.add(InlineKeyboardButton("🗑️ حذف المهمة", callback_data=f"admin_task_delete|{t_id}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="admin_tasks"))
            bot.edit_message_text(
                f"📌 *{safe_title}*\n\n{safe_desc or 'بدون وصف'}\n\n"
                f"💰 المكافأة: {reward} نقطة ⭐\n"
                f"الحالة: {status_label}\n"
                f"👥 عدد المنجزين: {completions}",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        elif data.startswith("admin_task_toggle|"):
            if not is_admin(tg_id):
                return
            t_id = int(data.split("|", 1)[1])
            toggle_task_active(t_id)
            log_admin_action(tg_id, "toggle_task", f"task_id={t_id}")
            call.data = f"admin_task_view|{t_id}"
            callback_handler(call)
            return

        elif data.startswith("admin_task_delete|"):
            if not is_admin(tg_id):
                return
            t_id = int(data.split("|", 1)[1])
            delete_task(t_id)
            log_admin_action(tg_id, "delete_task", f"task_id={t_id}")
            bot.answer_callback_query(call.id, "🗑️ تم حذف المهمة.")
            call.data = "admin_tasks"
            callback_handler(call)
            return

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
                f"💰 إجمالي الرصيد: {tb:.1f} نقطة ⭐",
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
                safe_username = escape_markdown(username) if username else "بدون"
                join_date_display = join_date[:10] if join_date else "غير معروف"
                text += f"🆔 {uid} | @{safe_username}\n💰 {balance:.1f} | {status}\n📅 {join_date_display}\n\n"
            # 🔒 حد تيليجرام لطول الرسالة 4096 حرف - نقصّ احتياطاً لتفادي فشل الإرسال
            if len(text) > 4000:
                text = text[:4000] + "\n…(تم القص لتجاوز الحد المسموح)"
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
                f"الرصيد الحالي: {user[2]} نقطة ⭐\n"
                f"العملية: {op_labels.get(op, op)}\n\n"
                f"أرسل القيمة (رقم):",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        # ========== حظر / فك حظر مستخدم ==========
        elif data == "admin_ban_user":
            if not is_admin(tg_id):
                return
            user_states[tg_id] = "waiting_ban_user_id"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 إلغاء", callback_data="admin_panel"))
            bot.edit_message_text(
                "🚫 *حظر / فك حظر مستخدم*\n\n"
                "أرسل الآن User ID (معرّف المستخدم الرقمي) فقط:",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        elif data.startswith("banadmin_op|"):
            if not is_admin(tg_id):
                return
            _, target_id_str, op = data.split("|")
            target_id = int(target_id_str)
            if is_admin(target_id):
                bot.answer_callback_query(call.id, "❌ لا يمكن حظر إدمن.", show_alert=True)
                return
            user = get_user(target_id)
            if not user:
                bot.answer_callback_query(call.id, "❌ المستخدم غير مسجل في البوت.", show_alert=True)
                return
            if op == "unban":
                unban_user(target_id)
                log_admin_action(tg_id, "unban_user", f"target={target_id}")
                bot.edit_message_text(f"✅ تم فك الحظر عن المستخدم `{target_id}`.",
                                      chat_id, message_id, parse_mode='Markdown')
                try:
                    bot.send_message(target_id, "✅ تم فك حظرك، يمكنك استخدام البوت الآن.")
                except Exception:
                    pass
            else:  # ban
                user_states[tg_id] = f"waiting_ban_reason|{target_id}"
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("🔙 إلغاء", callback_data="admin_panel"))
                bot.edit_message_text(
                    f"🚫 المستخدم: `{target_id}`\n\n"
                    f"أرسل سبب الحظر (أو أرسل - لتجاهل كتابة سبب):",
                    chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
                )

        # ========== إنشاء رابط هدية (إدمن فقط) ==========
        elif data == "create_gift_link":
            if not is_admin(tg_id):
                return
            user_states[tg_id] = "waiting_gift_uses"
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("🔙 إلغاء", callback_data="admin_panel"))
            bot.edit_message_text(
                "🎁 *إنشاء رابط هدية*\n\n"
                "أرسل عدد المستخدمين الذين يمكنهم استخدام هذا الرابط (رقم صحيح):",
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
                safe_action = escape_markdown(action) if action else ""
                safe_details = escape_markdown(details) if details else ""
                text += f"• {safe_action}: {safe_details}\n🕐 {created_at[:16]}\n\n"
            if len(text) > 4000:
                text = text[:4000] + "\n…(تم القص لتجاوز الحد المسموح)"
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
            backup_path = None
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
            finally:
                # الملف محفوظ بأمان داخل الدردشة الآن، لا حاجة لإبقاء نسخة محلية
                if backup_path and os.path.exists(backup_path):
                    try:
                        os.remove(backup_path)
                    except Exception:
                        pass

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
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(InlineKeyboardButton("🔧 إعدادات عامة", callback_data="settings_general"))
            keyboard.add(InlineKeyboardButton("🎁 إعدادات الهدية اليومية", callback_data="settings_daily_gift"))
            keyboard.add(InlineKeyboardButton("💸 إعدادات التحويل", callback_data="settings_transfer"))
            keyboard.add(InlineKeyboardButton("🏆 إعدادات لوحة المتصدرين", callback_data="settings_leaderboard"))
            keyboard.add(InlineKeyboardButton("📋 إعدادات المهام", callback_data="settings_tasks"))
            keyboard.add(InlineKeyboardButton("🤖 إعدادات البحث الذكي", callback_data="settings_ai_search"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel"))
            bot.edit_message_text("⚙️ *الإعدادات*\n\nاختر القسم:", chat_id, message_id,
                                  parse_mode='Markdown', reply_markup=keyboard)

        elif data == "settings_general":
            if not is_admin(tg_id):
                return
            settings = {
                'الإحالات للطلب': 'referrals_for_feature',
                'الإحالات للـVIP': 'referrals_for_vip',
                'تكلفة الطلب (نقاط)': 'request_cost_points',
                'تكلفة VIP (نقاط)': 'vip_cost_points',
            }
            keyboard = InlineKeyboardMarkup(row_width=1)
            for label, key in settings.items():
                val = get_setting(key)
                keyboard.add(InlineKeyboardButton(f"{label}: {val}", callback_data=f"editsetting|{key}"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="edit_settings"))
            bot.edit_message_text("🔧 *الإعدادات العامة*", chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)

        elif data == "settings_daily_gift":
            if not is_admin(tg_id):
                return
            enabled = get_setting('daily_gift_enabled') == '1'
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(InlineKeyboardButton(
                "✅ مفعّلة (اضغط للإيقاف)" if enabled else "❌ متوقفة (اضغط للتفعيل)",
                callback_data="toggle_feature|daily_gift_enabled"
            ))
            keyboard.add(InlineKeyboardButton(f"💰 القيمة الأساسية: {get_setting('daily_gift_base')} نقطة ⭐",
                                              callback_data="editsetting|daily_gift_base"))
            keyboard.add(InlineKeyboardButton(f"📈 الزيادة لكل يوم: {get_setting('daily_gift_increment')} نقطة ⭐",
                                              callback_data="editsetting|daily_gift_increment"))
            keyboard.add(InlineKeyboardButton(f"🔝 أقصى تتالي: {get_setting('daily_gift_max_streak')} يوم",
                                              callback_data="editsetting|daily_gift_max_streak"))
            keyboard.add(InlineKeyboardButton(f"🎉 بونص الإحالة اليومي: {get_setting('daily_gift_referral_bonus_percent')}%",
                                              callback_data="editsetting|daily_gift_referral_bonus_percent"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="edit_settings"))
            bot.edit_message_text(
                "🎁 *إعدادات الهدية اليومية*\n\n"
                "القيمة = الأساسية + (الزيادة × (أيام التتالي - 1))، حتى أقصى تتالي.\n"
                "إن أحال المستخدم شخصاً واكتملت إحالته في نفس اليوم، يُضاف بونص % لمرة واحدة فقط في اليوم.",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        elif data == "settings_transfer":
            if not is_admin(tg_id):
                return
            enabled = get_setting('transfer_enabled') == '1'
            tax_enabled = get_setting('transfer_tax_enabled') == '1'
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(InlineKeyboardButton(
                "✅ التحويل مفعّل (اضغط للإيقاف)" if enabled else "❌ التحويل متوقف (اضغط للتفعيل)",
                callback_data="toggle_feature|transfer_enabled"
            ))
            keyboard.add(InlineKeyboardButton(
                "✅ الضريبة مفعّلة (اضغط للإيقاف)" if tax_enabled else "❌ الضريبة متوقفة (اضغط للتفعيل)",
                callback_data="toggle_feature|transfer_tax_enabled"
            ))
            keyboard.add(InlineKeyboardButton(f"💸 نسبة الضريبة: {get_setting('transfer_tax_percent')}%",
                                              callback_data="editsetting|transfer_tax_percent"))
            keyboard.add(InlineKeyboardButton(f"🔢 أقل مبلغ للتحويل: {get_setting('transfer_min_amount')} نقطة ⭐",
                                              callback_data="editsetting|transfer_min_amount"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="edit_settings"))
            bot.edit_message_text(
                "💸 *إعدادات التحويل*\n\n"
                "مثال: تحويل 10 نقطة ⭐ بضريبة 20% ← المستلم يستلم 8 نقطة ⭐.",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        elif data == "settings_leaderboard":
            if not is_admin(tg_id):
                return
            enabled = get_setting('leaderboard_enabled') == '1'
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(InlineKeyboardButton(
                "✅ مفعّلة (اضغط للإيقاف)" if enabled else "❌ متوقفة (اضغط للتفعيل)",
                callback_data="toggle_feature|leaderboard_enabled"
            ))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="edit_settings"))
            bot.edit_message_text("🏆 *إعدادات لوحة المتصدرين*\n\nترتيب حسب عدد الإحالات الناجحة.",
                                  chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard)

        elif data == "settings_tasks":
            if not is_admin(tg_id):
                return
            enabled = get_setting('tasks_enabled') == '1'
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(InlineKeyboardButton(
                "✅ مفعّلة (اضغط للإيقاف)" if enabled else "❌ متوقفة (اضغط للتفعيل)",
                callback_data="toggle_feature|tasks_enabled"
            ))
            keyboard.add(InlineKeyboardButton("📋 إدارة المهام", callback_data="admin_tasks"))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="edit_settings"))
            bot.edit_message_text("📋 *إعدادات المهام*", chat_id, message_id,
                                  parse_mode='Markdown', reply_markup=keyboard)

        elif data == "settings_ai_search":
            if not is_admin(tg_id):
                return
            enabled = get_setting('ai_search_enabled') == '1'
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(InlineKeyboardButton(
                "✅ مفعّل (اضغط للإيقاف)" if enabled else "❌ متوقف (اضغط للتفعيل)",
                callback_data="toggle_feature|ai_search_enabled"
            ))
            keyboard.add(InlineKeyboardButton(
                f"⚡ أقصى طلبات متزامنة: {get_setting('ai_search_max_concurrent')}",
                callback_data="editsetting|ai_search_max_concurrent"
            ))
            keyboard.add(InlineKeyboardButton(
                f"⏱️ مهلة الطلب (ثانية): {get_setting('ai_search_timeout_seconds')}",
                callback_data="editsetting|ai_search_timeout_seconds"
            ))
            keyboard.add(InlineKeyboardButton(
                f"🔡 أدنى أحرف للبحث: {get_setting('ai_search_min_chars') or '2'}",
                callback_data="editsetting|ai_search_min_chars"
            ))
            keyboard.add(InlineKeyboardButton(
                f"🔠 أقصى أحرف للبحث: {get_setting('ai_search_max_chars') or '80'}",
                callback_data="editsetting|ai_search_max_chars"
            ))
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data="edit_settings"))
            bot.edit_message_text(
                "🤖 *إعدادات البحث بالذكاء الصناعي*\n\n"
                "تطبيقات VIP مستبعدة دائماً من نتائج هذا البحث مهما كانت الإعدادات.\n"
                "الكلمات المفتاحية تُضاف من داخل بطاقة كل تطبيق (للإدمن فقط).",
                chat_id, message_id, parse_mode='Markdown', reply_markup=keyboard
            )

        elif data.startswith("toggle_feature|"):
            if not is_admin(tg_id):
                return
            key = data.split("|", 1)[1]
            current = get_setting(key)
            new_val = '0' if current == '1' else '1'
            set_setting(key, new_val)
            log_admin_action(tg_id, "toggle_feature", f"{key} = {new_val}")
            bot.answer_callback_query(call.id, "✅ تم التحديث.")
            # نعيد فتح نفس قسم الإعدادات الذي جاء منه الزر
            section_map = {
                'daily_gift_enabled': 'settings_daily_gift',
                'transfer_enabled': 'settings_transfer',
                'transfer_tax_enabled': 'settings_transfer',
                'leaderboard_enabled': 'settings_leaderboard',
                'tasks_enabled': 'settings_tasks',
                'ai_search_enabled': 'settings_ai_search',
            }
            call.data = section_map.get(key, "edit_settings")
            callback_handler(call)
            return

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
            user_id_r, typ_r, app_name_r, desc_r, file_id_r = req
            if typ_r == 'crack' and file_id_r and file_id_r.startswith('CRACK_CHAN:'):
                crack_msg_id = int(file_id_r.split(':')[1])
                try:
                    bot.copy_message(
                        chat_id=tg_id,
                        from_chat_id=DB_CHANNEL_ID,
                        message_id=crack_msg_id,
                        protect_content=True,
                        caption=f"🔒 ملف APK | طلب #{req_id} | مستخدم: {user_id_r}"
                    )
                except Exception as _e:
                    bot.send_message(tg_id, f"⚠️ تعذّر استرداد الملف من القناة: {_e}")
                text = (f"🔨 *طلب كسر #{req_id}*\n"
                        f"المستخدم: {user_id_r}\n"
                        f"الملف: {app_name_r}\n"
                        f"_الملف أُرسل أعلاه (محمي)_")
                bot.send_message(tg_id, text, parse_mode='Markdown', reply_markup=keyboard)
            else:
                text = (f"طلب #{req_id}\nالمستخدم: {user_id_r}\n"
                        f"النوع: {typ_r}\nالاسم: {app_name_r}\n"
                        f"الوصف: {desc_r or 'لا يوجد'}")
                bot.edit_message_text(text, chat_id, message_id, reply_markup=keyboard)

        elif data.startswith("approve_req_"):
            if not is_admin(tg_id):
                return
            req_id = int(data.split("_")[2])
            conn = db_conn()
            c = conn.cursor()
            # 🔒 إصلاح خطأ حقيقي: الإدراج السابق لم يكن يتحقق من الحالة الحالية
            # للطلب، فلو ضُغط الزر مرتين (دبل-تاب أو رسالة قديمة بعد معالجة
            # سابقة)، أو لو عُولج الطلب من قبل reject_req_ ثم approve_req_،
            # كان يُرسل إشعار "تمت الموافقة" للمستخدم أكثر من مرة ويُسجَّل في
            # سجل الإدمن مرتين لنفس الطلب. UPDATE ... WHERE status='pending'
            # ذرّي: ينجح فقط إن كان الطلب لا يزال معلقاً فعلاً.
            c.execute("UPDATE user_requests SET status='approved', admin_feedback='تمت الموافقة' "
                      "WHERE id=? AND status='pending'", (req_id,))
            if c.rowcount == 0:
                conn.commit()
                conn.close()
                bot.answer_callback_query(call.id, "⚠️ تمت معالجة هذا الطلب مسبقاً.", show_alert=True)
                return
            c.execute("SELECT user_id, type, app_name FROM user_requests WHERE id=?", (req_id,))
            req = c.fetchone()
            conn.commit()
            conn.close()
            if req:
                user_id, typ, name = req
                if typ == 'crack':
                    bot.send_message(user_id, f"✅ تم الموافقة على طلب كسر '{name}'.")
                else:
                    bot.send_message(user_id, f"✅ تم قبول رفع '{name}'. سيتم نشره قريباً.")
                log_admin_action(tg_id, "approve_request", f"{req_id} - {name}")
            bot.edit_message_text("✅ تمت الموافقة.", chat_id, message_id,
                                  reply_markup=InlineKeyboardMarkup().add(
                                      InlineKeyboardButton("🔙 رجوع", callback_data="view_requests")))

        elif data.startswith("reject_req_"):
            if not is_admin(tg_id):
                return
            req_id = int(data.split("_")[2])
            conn = db_conn()
            c = conn.cursor()
            # 🔒 إصلاح خطأ مالي حقيقي: بدون هذا الفحص الذرّي، ضغط مزدوج على
            # "❌ رفض" (أو رفض طلب سبقت الموافقة عليه عبر رسالة قديمة) كان
            # يعيد الرصيد المخصوم للمستخدم أكثر من مرة لنفس الطلب الواحد -
            # أي خصم مزدوج فعلي من رصيد البوت (المستخدم يربح نقاطاً لم تُخصم
            # فعلياً مرة ثانية). UPDATE ... WHERE status='pending' يضمن أن
            # هذا التحديث (وبالتالي الاسترجاع المالي) ينجح مرة واحدة فقط.
            c.execute("UPDATE user_requests SET status='rejected', admin_feedback='تم الرفض' "
                      "WHERE id=? AND status='pending'", (req_id,))
            if c.rowcount == 0:
                conn.commit()
                conn.close()
                bot.answer_callback_query(call.id, "⚠️ تمت معالجة هذا الطلب مسبقاً.", show_alert=True)
                return
            c.execute("SELECT user_id, app_name, deducted_amount FROM user_requests WHERE id=?", (req_id,))
            row = c.fetchone()
            conn.commit()
            conn.close()
            if row:
                user_id, name, deducted_amount = row
                # 🔧 إصلاح: كانت تُعيد دائماً 1.0 ثابتة بغض النظر عن المبلغ
                # الفعلي المخصوم وقت الطلب (الذي يتغيّر لو عدّل الإدمن إعداد
                # referrals_for_feature بين إنشاء الطلب ورفضه) - الآن تُعيد
                # المبلغ الحقيقي المخزَّن، أو 1.0 كقيمة احتياطية للطلبات
                # القديمة المُنشأة قبل إضافة هذا العمود.
                refund = deducted_amount if deducted_amount else 1.0
                update_balance(user_id, refund)
                bot.send_message(user_id, f"❌ تم رفض طلب '{name}'، تم إعادة رصيدك ({refund} نقطة ⭐).")
                log_admin_action(tg_id, "reject_request", f"{req_id} - {name} - استرجاع {refund} نقطة ⭐")
            bot.edit_message_text("❌ تم الرفض وإعادة الرصيد.", chat_id, message_id,
                                  reply_markup=InlineKeyboardMarkup().add(
                                      InlineKeyboardButton("🔙 رجوع", callback_data="view_requests")))

        # ========== الاشتراكات المعلقة (آخر 5 فقط) ==========
        elif data == "show_pending_referrals":
            conn = db_conn()
            c = conn.cursor()
            c.execute("""
                SELECT referred_id
                FROM referrals
                WHERE referrer_id=? AND status='pending'
                ORDER BY id DESC LIMIT 5
            """, (tg_id,))
            rows = c.fetchall()
            c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='pending'", (tg_id,))
            total_pending = c.fetchone()[0]
            conn.close()

            if not rows:
                bot.answer_callback_query(call.id, "لا توجد اشتراكات معلقة حالياً.")
                return

            lines = []
            for (referred_id_r,) in rows:
                r_username = _get_username(referred_id_r)
                r_link = _user_link(referred_id_r, r_username)
                lines.append(f"• {r_link} — 🔄 في انتظار الاشتراك بالقناة")

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

    # استخراج كود الإحالة أو كود رابط الهدية إن وُجد
    referrer_id = None
    has_ref_payload = False
    gift_code = None
    if ' ' in message.text:
        payload = message.text.split(' ', 1)[1].strip()
        if payload.startswith('ref_'):
            ref_code = payload[4:]
            resolved = resolve_referral_code(ref_code)
            if resolved and resolved != tg_id:
                referrer_id = resolved
                has_ref_payload = True
        elif payload.startswith('gift_'):
            gift_code = payload[5:]

    # محاولة التسجيل — تُعيد True فقط إذا كان مستخدماً جديداً حقاً
    is_new = register_user(tg_id, username, referrer_id)

    # 🚫 المستخدم المحظور: يُمنع من إعادة استخدام البوت حتى عبر /start
    if not is_admin(tg_id) and is_user_banned(tg_id):
        bot.send_message(tg_id, "🚫 تم حظرك من استخدام هذا البوت.")
        return

    if get_setting('maintenance_mode') == '1' and not is_admin(tg_id):
        bot.send_message(tg_id, "🔧 البوت في وضع الصيانة، عذراً.")
        return

    def welcome_and_maybe_gift():
        """يرسل رسالة الترحيب، وإن وُجد كود هدية صالح (من هذا /start أو من ضغطة سابقة) يصرفه فوراً."""
        nonlocal gift_code
        if not gift_code:
            gift_code = pending_gift_codes.pop(tg_id, None)
        else:
            pending_gift_codes.pop(tg_id, None)
        last_force_sub_prompt.pop(tg_id, None)
        # 🔒 إصلاح خطأ حقيقي: الصيغة السابقة بنت رابطاً بصيغة Markdown
        # [الاسم](tg://...)، لكن first_name يمكن أن يحتوي أي رمز يونيكود
        # تقريباً (بما فيها ']')، وMarkdown القديم لا يهرّب ']' أصلاً (فقط
        # '_' '*' '`' '['). فلو احتوى اسم المستخدم على ']' غير متوازنة،
        # كانت بنية الرابط تنكسر فيفشل send_message بالكامل برسالة خطأ
        # "Can't parse entities" - أي أن المستخدم لا يستلم رسالة الترحيب
        # ولا القائمة الرئيسية إطلاقاً ويبدو له أن البوت معطّل. الآن نستخدم
        # HTML mention (نفس أسلوب _user_link المستخدم بأمان في باقي الكود)
        # الذي يتطلب تهريب 3 رموز فقط (< > &) وهو أكثر متانة بكثير.
        display_name = (message.from_user.first_name or username)
        safe_display_name = (display_name.replace('&', '&amp;')
                                          .replace('<', '&lt;')
                                          .replace('>', '&gt;'))
        name_link = f'<a href="tg://user?id={tg_id}">{safe_display_name}</a>'
        bot.send_message(
            tg_id,
            f"مرحباً بك {name_link} 🚀",
            parse_mode='HTML',
            reply_markup=main_menu_keyboard(tg_id)
        )
        if gift_code:
            _try_redeem_gift(tg_id, gift_code)

    # ── المستخدم القديم (سبق أن ضغط /start من قبل) ─────────────────────
    # لا تُحتسب له أي إحالة، ويُعامَل دائماً كدخول عادي بغض النظر عن الرابط
    if not is_new:
        if not check_subscription(tg_id):
            if gift_code:
                pending_gift_codes[tg_id] = gift_code
            send_force_sub_prompt(tg_id)
            return
        welcome_and_maybe_gift()
        return

    # ── مستخدم جديد دخل من رابط إحالة ──────────────────────────────────
    if has_ref_payload and referrer_id:
        if not check_subscription(tg_id):
            # غير مشترك → رسالة الإحالة المخصصة
            add_referral(referrer_id, tg_id)   # نسجّل الإحالة pending الآن
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
            return
        else:
            # مشترك → سجّل الإحالة ثم شغّل المعالج فوراً
            add_referral(referrer_id, tg_id)
            threading.Thread(target=process_pending_referrals, daemon=True).start()
            welcome_and_maybe_gift()
            return

    # ── مستخدم جديد دخل برابط هدية (بدون رابط إحالة) ──────────────────
    if gift_code and not check_subscription(tg_id):
        # غير مشترك → يجب الاشتراك أولاً قبل الاستفادة من رابط الهدية
        pending_gift_codes[tg_id] = gift_code
        old_msg_id = last_force_sub_prompt.get(tg_id)
        if old_msg_id:
            try:
                bot.delete_message(tg_id, old_msg_id)
            except Exception:
                pass
        sent = bot.send_message(
            tg_id,
            "🎁 يرجى الاشتراك في القناة أولاً للاستفادة من رابط الهدية:",
            reply_markup=force_sub_keyboard()
        )
        last_force_sub_prompt[tg_id] = sent.message_id
        return

    # ── مستخدم جديد دخل بدون رابط إحالة ───────────────────────────────
    if not check_subscription(tg_id):
        send_force_sub_prompt(tg_id)
        return

    welcome_and_maybe_gift()

def _try_redeem_gift(tg_id: int, gift_code: str):
    """يصرف رابط الهدية ويُعلم المستخدم بالنتيجة. يُستدعى فقط بعد التأكد من الاشتراك."""
    result = redeem_gift_link(gift_code, tg_id)
    if result == "ok":
        info = get_gift_link(gift_code)
        points = info[1] if info else None
        points_txt = f"{points:.2f} نقطة ⭐" if points is not None else ""
        bot.send_message(tg_id, f"🎁 تم تفعيل رابط الهدية بنجاح! تم إضافة {points_txt} لرصيدك.")
    elif result == "already":
        bot.send_message(tg_id, "ℹ️ سبق أن استخدمت رابط الهدية هذا.")
    elif result == "exhausted":
        bot.send_message(tg_id, "❌ انتهت صلاحية رابط الهدية (نفدت عدد الاستخدامات).")
    elif result == "not_found":
        bot.send_message(tg_id, "❌ رابط الهدية غير صالح.")

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

    # 🔒 إصلاح بسيط: لو كان هناك ملف استعادة سابق لم يُعالَج بعد (لم يُؤكَّد
    # ولم يُلغَ)، نحذفه الآن قبل استبداله - وإلا يبقى ملفاً يتيماً على القرص
    # للأبد (تسريب تخزين بطيء عند تكرار رفع ملفات دون تأكيد/إلغاء).
    previous_pending = pending_db_restores.get(tg_id)
    if previous_pending and previous_pending != download_path and os.path.exists(previous_pending):
        try:
            os.remove(previous_pending)
        except Exception:
            pass

    pending_db_restores[tg_id] = download_path
    user_states.pop(tg_id, None)
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(InlineKeyboardButton("✅ تأكيد الاستبدال", callback_data="confirm_db_restore"))
    keyboard.add(InlineKeyboardButton("❌ إلغاء", callback_data="cancel_db_restore"))
    # 🔒 نزيل أي backtick من اسم الملف فقط عند العرض (نادر لكن قد يكسر الـcode span)
    safe_file_name = file_name.replace("`", "'")
    bot.reply_to(message,
        "⚠️ *تأكيد الاستبدال*\n\n"
        f"الملف `{safe_file_name}` تم استلامه وفحصه بنجاح.\n\n"
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
    safe_file_name = escape_markdown(file_name)
    bot.reply_to(message, f"✅ استلمت الملف: *{safe_file_name}*\n\n✏️ أرسل وصف التطبيق (أو أرسل /skip للتخطي):",
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

    # 🔒 الاسم والوصف نصوص حرة (من اسم الملف أو كتابة الإدمن) وقد تحتوي رموز
    # Markdown خاصة (مثل _) تكسر التنسيق وتفشل عملية النشر بالكامل. نُهرّبها
    # هنا فقط للعرض، بينما نُخزّن name الأصلي بدون تهريب في قاعدة البيانات
    # (register_app_in_channel أدناه) لأن التهريب يجب أن يحصل وقت العرض فقط.
    safe_name = escape_markdown(name)
    safe_description = escape_markdown(description)

    caption_text = f"📱 *{safe_name}*"
    if safe_description:
        caption_text += f"\n\n{safe_description}"
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
        elif original_message.photo:
            # 🔧 إصلاح: content_types في receive_app_file تسمح صراحة بـ 'photo'،
            # لكن هذا الفرع كان مفقوداً هنا فيرفض الصورة بعد أن يكون الإدمن قد
            # أضاع وقته في كتابة الوصف. نأخذ أعلى دقة متاحة (آخر عنصر بالقائمة).
            sent = bot.send_photo(
                DB_CHANNEL_ID,
                original_message.photo[-1].file_id,
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
            f"📱 *{safe_name}*\n"
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
    safe_old_name = escape_markdown(old_name)
    safe_new_name = escape_markdown(new_name)
    bot.reply_to(message,
        f"✅ تم تعديل الاسم الظاهر بنجاح:\n"
        f"من: *{safe_old_name}*\n"
        f"إلى: *{safe_new_name}*\n\n"
        f"ملاحظة: اسم الملف الأصلي لم يتغيّر.",
        parse_mode='Markdown', reply_markup=keyboard
    )

# ========== استقبال الكلمات المفتاحية (للبحث بالذكاء الصناعي فقط) ==========
@bot.message_handler(func=lambda m: is_admin(m.from_user.id)
                     and isinstance(user_states.get(m.from_user.id, ""), str)
                     and user_states.get(m.from_user.id, "").startswith("waiting_app_keywords|"))
def receive_app_keywords(message):
    tg_id = message.from_user.id
    state = user_states[tg_id]
    app_code = state.split("|", 1)[1]

    raw = message.text.strip() if message.text else ""
    # السماح بمسح الكلمات المفتاحية بإرسال "-" أو "حذف"
    new_keywords = None if raw in ["-", "حذف", "مسح"] else raw
    if not new_keywords and raw not in ["-", "حذف", "مسح"]:
        bot.reply_to(message, "❌ أرسل نصاً، أو أرسل - لحذف الكلمات المفتاحية الحالية.")
        return

    info = get_app_info(app_code)
    if not info:
        bot.reply_to(message, "❌ التطبيق لم يعد موجوداً.")
        user_states.pop(tg_id, None)
        return

    set_app_keywords(app_code, new_keywords)
    log_admin_action(tg_id, "set_app_keywords", f"app_code={app_code}")
    user_states.pop(tg_id, None)

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 رجوع للتطبيق", callback_data=f"appv_{app_code}"))
    status_text = "تم حذف الكلمات المفتاحية." if not new_keywords else f"تم الحفظ:\n*{escape_markdown(new_keywords)}*"
    bot.reply_to(message,
        f"✅ {status_text}",
        parse_mode='Markdown', reply_markup=keyboard
    )

# ========== البحث ==========
@bot.message_handler(func=lambda m: user_states.get(m.from_user.id) == "waiting_ai_search_query")
def receive_ai_search_query(message):
    tg_id = message.from_user.id
    # 🔒 نفس دفاع البحث القديم: لو حُظر المستخدم بعد دخوله الحالة وقبل الإرسال
    if not is_admin(tg_id) and is_user_banned(tg_id):
        user_states.pop(tg_id, None)
        bot.reply_to(message, "🚫 تم حظرك من استخدام البوت.")
        return

    query = message.text.strip() if message.text else ""
    min_chars = int(float(get_setting('ai_search_min_chars') or 2))
    max_chars = int(float(get_setting('ai_search_max_chars') or 80))
    if len(query) < min_chars:
        bot.reply_to(message, f"❌ اكتب وصفاً لا يقل عن {min_chars} أحرف.")
        return
    if len(query) > max_chars:
        bot.reply_to(message, f"❌ الوصف طويل جداً (الحد الأقصى {max_chars} حرف).")
        return

    user_states.pop(tg_id, None)
    thinking_msg = bot.reply_to(message, "🤖 جاري البحث...")
    _ai_user_id = tg_id
    _ai_username = message.from_user.username or ''

    def run_search():
        app_code = ai_search_app(query)
        try:
            if not app_code:
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="main_menu"))
                bot.edit_message_text(
                    "❌ لم أجد نتيجة مطابقة، يرجى البحث يدوياً عبر التصنيفات.",
                    tg_id, thinking_msg.message_id, reply_markup=keyboard
                )
                return

            info = get_app_info(app_code)
            if not info:
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="main_menu"))
                bot.edit_message_text(
                    "❌ لم أجد نتيجة مطابقة، يرجى البحث يدوياً عبر التصنيفات.",
                    tg_id, thinking_msg.message_id, reply_markup=keyboard
                )
                # تسجيل في القناة: لا نتيجة
                try:
                    _un_log = f'@{_ai_username}' if _ai_username else str(_ai_user_id)
                    bot.send_message(
                        DB_CHANNEL_ID,
                        f'🤖 سجل بحث ذكي\n'
                        f'👤 {_un_log}\n'
                        f'🔍 "{query}"\n'
                        f'❌ لا توجد نتيجة',
                        disable_notification=True
                    )
                except Exception:
                    pass
                return

            # تسجيل في القناة: وُجدت نتيجة
            try:
                _un_log = f'@{_ai_username}' if _ai_username else str(_ai_user_id)
                bot.send_message(
                    DB_CHANNEL_ID,
                    f'🤖 سجل بحث ذكي\n'
                    f'👤 {_un_log}\n'
                    f'🔍 "{query}"\n'
                    f'📱 {info[4]}',
                    disable_notification=True
                )
            except Exception:
                pass

            # نعرض بطاقة التطبيق بنفس الشكل والمسار المستخدم عند الضغط على
            # تطبيق من القائمة يدوياً (appv_{app_code})، بإعادة استخدام نفس
            # المعالج الأصلي بدل تكرار منطق بناء البطاقة وزر التحميل المشفّر.
            # 🔒 نتحقق من rate-limit هنا مسبقاً (بدل تركها لمعالج appv_) لأن
            # ذاك المعالج يستدعي bot.answer_callback_query(call.id, ...) عند
            # الفشل، وهذا الاستدعاء الداخلي ليس له callback_query_id حقيقي
            # من تيليجرام، فسيفشل بصمت غير آمن لو دخل ذلك الفرع.
            if is_rate_limited(tg_id):
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="main_menu"))
                bot.edit_message_text(
                    "⏱️ محاولات كثيرة، حاول لاحقاً بعد دقيقة.",
                    tg_id, thinking_msg.message_id, reply_markup=keyboard
                )
                return

            class _FakeCall:
                pass
            fake_call = _FakeCall()
            fake_call.id = "ai_search_internal"
            fake_call.data = f"appv_{app_code}"
            fake_call.from_user = message.from_user
            fake_call.message = thinking_msg
            callback_handler(fake_call)
        except Exception as e:
            print(f"AI search delivery error: {e}")
            try:
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="main_menu"))
                bot.edit_message_text(
                    "❌ حدث خطأ أثناء جلب التطبيق، حاول مرة أخرى.",
                    tg_id, thinking_msg.message_id, reply_markup=keyboard
                )
            except Exception:
                pass

    threading.Thread(target=run_search, daemon=True).start()



# ========== طلب كسر — استقبال ملف APK/APKs ==========
@bot.message_handler(
    content_types=['document'],
    func=lambda m: user_states.get(m.from_user.id) == 'waiting_crack_apk_file'
)
def receive_crack_apk_file(message):
    tg_id = message.from_user.id
    if not is_admin(tg_id) and is_user_banned(tg_id):
        user_states.pop(tg_id, None)
        bot.reply_to(message, '🚫 تم حظرك من استخدام البوت.')
        return

    # التحقق من نوع الملف (APK أو APKs فقط)
    file_name = (message.document.file_name or '').strip()
    ext = file_name.lower().rsplit('.', 1)[-1] if '.' in file_name else ''
    if ext not in ('apk', 'apks'):
        bot.reply_to(
            message,
            '❌ الملف غير مقبول.\n'
            'يُقبل فقط ملفات APK أو APKS.\n'
            'أرسل الملف بالامتداد الصحيح (.apk أو .apks):'
        )
        return   # لا نُلغي الـ state حتى يتمكن من إعادة الإرسال

    required = float(get_setting('request_cost_points') or 2)
    # 🔒 خصم ذرّي يمنع الخصم المزدوج عند الضغط المتزامن
    if not try_deduct_balance(tg_id, required):
        bot.reply_to(message, '❌ رصيدك غير كافٍ.')
        user_states.pop(tg_id, None)
        return

    # 🔒 إخفاء هوية المستخدم: كابشن مشفّر لا يحتوي أي بيانات شخصية
    encrypted_ref = hashlib.sha256(
        f"{SECRET_KEY}:CRACK:{tg_id}:{time.time()}".encode()
    ).hexdigest()[:24]

    # إرسال الملف إلى قناة DB بـ copy_message (بدون هيدر "محوّل من")
    # protect_content=True يمنع أي شخص من تحويل الملف خارج القناة
    try:
        sent_to_channel = bot.copy_message(
            chat_id=DB_CHANNEL_ID,
            from_chat_id=tg_id,
            message_id=message.message_id,
            caption=f'🔐 {encrypted_ref}',
            protect_content=True,
            disable_notification=True
        )
        crack_channel_msg_id = sent_to_channel.message_id
    except Exception as _e:
        update_balance(tg_id, required)  # إعادة الرصيد عند فشل الإرسال
        bot.reply_to(message, f'❌ فشل إرسال الملف، تم إعادة رصيدك.\nالخطأ: {_e}')
        user_states.pop(tg_id, None)
        return

    # حفظ الطلب في قاعدة البيانات
    # file_id يُستخدم هنا لتخزين message_id في القناة بصيغة CRACK_CHAN:{id}
    conn = db_conn()
    c = conn.cursor()
    c.execute(
        'INSERT INTO user_requests '
        '(user_id, type, app_name, status, created_at, deducted_amount, file_id) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (
            tg_id, 'crack', file_name,
            'pending', datetime.datetime.now().isoformat(),
            required, f'CRACK_CHAN:{crack_channel_msg_id}'
        )
    )
    conn.commit()
    conn.close()

    user_states.pop(tg_id, None)
    bot.reply_to(message, '✅ تم إرسال طلب الكسر، سيتم مراجعته قريباً.')
    log_admin_action(tg_id, 'crack_request', f'file={file_name}, chan_msg={crack_channel_msg_id}')

# ========== طلب كسر — رسالة نصية خاطئة (توجيه المستخدم) ==========
@bot.message_handler(
    func=lambda m: user_states.get(m.from_user.id) == 'waiting_crack_apk_file'
)
def crack_wrong_type(message):
    """يُوجّه المستخدم إن أرسل نصاً أو ملفاً غير APK."""
    bot.reply_to(
        message,
        '❌ يُقبل فقط ملف APK أو APKS.\n'
        'أرسل الملف بامتداد .apk أو .apks مباشرةً:'
    )

# ========== رفع ملف (طلب مستخدم) ==========
@bot.message_handler(content_types=['document'],
                     func=lambda m: user_states.get(m.from_user.id) == "waiting_upload_file")
def handle_upload_request(message):
    tg_id = message.from_user.id
    # 🔒 دفاع ثانٍ: لو حُظر المستخدم بعد دخوله هذه الحالة وقبل إرسال الملف
    if not is_admin(tg_id) and is_user_banned(tg_id):
        user_states.pop(tg_id, None)
        bot.reply_to(message, "🚫 تم حظرك من استخدام البوت.")
        return
    required = float(get_setting('request_cost_points') or 2)
    # 🔒 نفس الحماية الذرّية المستخدمة في طلب الكسر
    if not try_deduct_balance(tg_id, required):
        bot.reply_to(message, "❌ رصيدك غير كافٍ.")
        user_states.pop(tg_id, None)
        return
    file_id = message.document.file_id
    file_name = message.document.file_name or "تطبيق"
    # 🔧 إصلاح: نُمرّر المبلغ الفعلي المخصوم (required) عبر الـ state، بدل أن
    # يُفترض لاحقاً قيمة ثابتة غير دقيقة عند إعادته إن رُفض الطلب.
    user_states[tg_id] = f"waiting_upload_desc|{file_id}|{file_name}|{required}"
    bot.reply_to(message, "✏️ أرسل وصف التطبيق:")

@bot.message_handler(func=lambda m: isinstance(user_states.get(m.from_user.id, ""), str)
                     and user_states.get(m.from_user.id, "").startswith("waiting_upload_desc|"))
def receive_upload_desc(message):
    tg_id = message.from_user.id
    state = user_states[tg_id]
    parts = state.split("|", 3)
    file_id = parts[1]
    file_name = parts[2]
    deducted_amount = float(parts[3]) if len(parts) > 3 else 1.0
    description = message.text
    conn = db_conn()
    c = conn.cursor()
    c.execute("INSERT INTO user_requests (user_id, type, app_name, description, file_id, status, created_at, deducted_amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (tg_id, 'upload', file_name, description, file_id, 'pending', datetime.datetime.now().isoformat(), deducted_amount))
    conn.commit()
    conn.close()
    bot.reply_to(message, "✅ تم إرسال طلب الرفع للإدمن.")
    user_states.pop(tg_id, None)
    bot.send_message(ADMIN_ID, f"📩 طلب رفع جديد من @{message.from_user.username or tg_id}\nالتطبيق: {file_name}")


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
    safe_username = escape_markdown(user[1]) if user[1] else "بدون"
    bot.reply_to(message,
        f"✅ المستخدم موجود.\n"
        f"🆔 `{target_id}` | @{safe_username}\n"
        f"💰 الرصيد الحالي: {user[2]} نقطة ⭐\n\n"
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
        f"💰 الرصيد الجديد: {new_balance} نقطة ⭐",
        parse_mode='Markdown', reply_markup=keyboard
    )

# ========== حظر/فك حظر: استقبال User ID ==========
@bot.message_handler(func=lambda m: is_admin(m.from_user.id)
                     and user_states.get(m.from_user.id) == "waiting_ban_user_id")
def receive_ban_target_id(message):
    tg_id = message.from_user.id
    raw = message.text.strip() if message.text else ""
    if not raw.isdigit():
        bot.reply_to(message, "❌ معرّف غير صالح، أرسل User ID رقمي فقط.")
        return
    target_id = int(raw)
    if is_admin(target_id):
        bot.reply_to(message, "❌ لا يمكن حظر إدمن.")
        user_states.pop(tg_id, None)
        return
    user = get_user(target_id)
    if not user:
        bot.reply_to(message, "❌ هذا المستخدم غير مسجل في البوت.")
        user_states.pop(tg_id, None)
        bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
        return

    user_states.pop(tg_id, None)
    currently_banned = bool(len(user) > 7 and user[7])
    keyboard = InlineKeyboardMarkup(row_width=1)
    if currently_banned:
        keyboard.add(InlineKeyboardButton("✅ فك الحظر", callback_data=f"banadmin_op|{target_id}|unban"))
    else:
        keyboard.add(InlineKeyboardButton("🚫 حظر المستخدم", callback_data=f"banadmin_op|{target_id}|ban"))
    keyboard.add(InlineKeyboardButton("🔙 إلغاء", callback_data="admin_panel"))
    safe_username = escape_markdown(user[1]) if user[1] else "بدون"
    status_text = "🚫 محظور حالياً" if currently_banned else "✅ غير محظور"
    bot.reply_to(message,
        f"المستخدم موجود.\n"
        f"🆔 `{target_id}` | @{safe_username}\n"
        f"الحالة: {status_text}\n\n"
        f"اختر العملية:",
        parse_mode='Markdown', reply_markup=keyboard
    )

# ========== حظر: استقبال السبب وتنفيذ الحظر ==========
@bot.message_handler(func=lambda m: is_admin(m.from_user.id)
                     and isinstance(user_states.get(m.from_user.id, ""), str)
                     and user_states.get(m.from_user.id, "").startswith("waiting_ban_reason|"))
def receive_ban_reason(message):
    tg_id = message.from_user.id
    state = user_states[tg_id]
    _, target_id_str = state.split("|")
    target_id = int(target_id_str)

    # 🔒 إعادة التحقق لحظة التنفيذ (دفاع ثانٍ)
    user = get_user(target_id)
    if not user or is_admin(target_id):
        bot.reply_to(message, "❌ تعذّر تنفيذ العملية. تم الإلغاء.")
        user_states.pop(tg_id, None)
        bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
        return

    raw_reason = message.text.strip() if message.text else ""
    reason = "" if raw_reason == "-" else raw_reason

    ban_user(target_id, reason)
    log_admin_action(tg_id, "ban_user", f"target={target_id}, reason={reason}")
    user_states.pop(tg_id, None)

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 رجوع للوحة الإدارة", callback_data="admin_panel"))
    bot.reply_to(message,
        f"✅ تم حظر المستخدم.\n"
        f"🆔 `{target_id}`\n"
        f"السبب: {reason or 'بدون سبب محدد'}",
        parse_mode='Markdown', reply_markup=keyboard
    )
    try:
        bot.send_message(target_id, "🚫 تم حظرك من استخدام هذا البوت.")
    except Exception:
        pass

# ========== إنشاء رابط هدية: استقبال عدد الاستخدامات ==========
@bot.message_handler(func=lambda m: is_admin(m.from_user.id)
                     and user_states.get(m.from_user.id) == "waiting_gift_uses")
def receive_gift_uses(message):
    tg_id = message.from_user.id
    raw = message.text.strip() if message.text else ""
    if not raw.isdigit() or int(raw) <= 0:
        bot.reply_to(message, "❌ أدخل رقماً صحيحاً أكبر من صفر لعدد المستخدمين.")
        return
    max_uses = int(raw)
    user_states[tg_id] = f"waiting_gift_points|{max_uses}"
    bot.reply_to(message, "💰 الآن أرسل عدد النقاط (نقطة ⭐) التي سيحصل عليها كل مستخدم يستخدم الرابط:")

# ========== إنشاء رابط هدية: استقبال عدد النقاط وإنشاء الرابط ==========
@bot.message_handler(func=lambda m: is_admin(m.from_user.id)
                     and isinstance(user_states.get(m.from_user.id, ""), str)
                     and user_states.get(m.from_user.id, "").startswith("waiting_gift_points|"))
def receive_gift_points(message):
    tg_id = message.from_user.id
    state = user_states[tg_id]
    max_uses = int(state.split("|", 1)[1])

    raw_value = message.text.strip() if message.text else ""
    try:
        points = float(raw_value)
    except ValueError:
        bot.reply_to(message, "❌ القيمة يجب أن تكون رقماً.")
        return
    if points <= 0:
        bot.reply_to(message, "❌ أدخل قيمة موجبة فقط.")
        return

    code = create_gift_link(points, max_uses, tg_id)
    user_states.pop(tg_id, None)
    log_admin_action(tg_id, "create_gift_link", f"code={code}, points={points}, max_uses={max_uses}")

    bu = BOT_USERNAME or bot.get_me().username
    gift_link = f"https://t.me/{bu}?start=gift_{code}"

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 رجوع للوحة الإدارة", callback_data="admin_panel"))
    bot.reply_to(message,
        f"✅ *تم إنشاء رابط الهدية بنجاح*\n\n"
        f"💰 النقاط لكل استخدام: {points} نقطة ⭐\n"
        f"👥 عدد الاستخدامات: {max_uses}\n\n"
        f"🔗 الرابط:\n`{gift_link}`\n\n"
        f"ملاحظة: أي مستخدم جديد كلياً يفتح الرابط سيُطلب منه الاشتراك بالقناة أولاً قبل استلام الهدية.",
        parse_mode='Markdown', reply_markup=keyboard
    )



# ========== تحويل النقاط: استقبال User ID المستلم ==========
@bot.message_handler(func=lambda m: user_states.get(m.from_user.id) == "waiting_transfer_target_id")
def receive_transfer_target_id(message):
    tg_id = message.from_user.id
    raw = message.text.strip() if message.text else ""
    if not raw.isdigit():
        bot.reply_to(message, "❌ أرسل User ID رقمي فقط.")
        return
    target_id = int(raw)

    if target_id == tg_id:
        bot.reply_to(message, "❌ لا يمكنك التحويل لنفسك.")
        user_states.pop(tg_id, None)
        bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
        return

    target_user = get_user(target_id)
    if not target_user:
        bot.reply_to(message, "❌ هذا المستخدم غير مسجل في البوت.")
        user_states.pop(tg_id, None)
        bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
        return
    if len(target_user) > 7 and target_user[7]:
        bot.reply_to(message, "❌ هذا المستخدم محظور، لا يمكن التحويل له.")
        user_states.pop(tg_id, None)
        bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
        return

    user_states[tg_id] = f"waiting_transfer_amount|{target_id}"
    bot.reply_to(message, f"💰 أرسل المبلغ الذي تريد تحويله للمستخدم `{target_id}`:", parse_mode='Markdown')

# ========== تحويل النقاط: استقبال المبلغ وعرض التأكيد ==========
@bot.message_handler(func=lambda m: isinstance(user_states.get(m.from_user.id, ""), str)
                     and user_states.get(m.from_user.id, "").startswith("waiting_transfer_amount|"))
def receive_transfer_amount(message):
    tg_id = message.from_user.id
    state = user_states[tg_id]
    target_id = int(state.split("|", 1)[1])

    raw = message.text.strip() if message.text else ""
    try:
        amount = float(raw)
    except ValueError:
        bot.reply_to(message, "❌ القيمة يجب أن تكون رقماً.")
        return
    if amount <= 0:
        bot.reply_to(message, "❌ أدخل قيمة موجبة فقط.")
        return

    min_amount = float(get_setting('transfer_min_amount') or 1)
    if amount < min_amount:
        bot.reply_to(message, f"❌ أقل مبلغ للتحويل هو {min_amount} نقطة ⭐.")
        return

    user = get_user(tg_id)
    if not user or user[2] < amount:
        bot.reply_to(message, "❌ رصيدك غير كافٍ.")
        user_states.pop(tg_id, None)
        bot.send_message(tg_id, "🏠 اختر من القائمة:", reply_markup=main_menu_keyboard(tg_id))
        return

    received, tax = calculate_transfer(amount)
    user_states[tg_id] = f"confirm_transfer|{target_id}|{amount}"

    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("✅ تأكيد التحويل", callback_data="transfer_confirm"))
    keyboard.add(InlineKeyboardButton("❌ إلغاء", callback_data="transfer_cancel"))

    tax_line = f"💸 الضريبة ({get_setting('transfer_tax_percent')}%): {tax} نقطة ⭐\n" if tax > 0 else ""
    bot.reply_to(message,
        f"📋 *تأكيد التحويل*\n\n"
        f"👤 المستلم: `{target_id}`\n"
        f"📤 المبلغ المُرسَل: {amount} نقطة ⭐\n"
        f"{tax_line}"
        f"📥 المبلغ الذي سيستلمه: {received} نقطة ⭐\n\n"
        f"هل تؤكد التحويل؟",
        parse_mode='Markdown', reply_markup=keyboard
    )

# ========== إضافة مهمة: استقبال العنوان ==========
@bot.message_handler(func=lambda m: is_admin(m.from_user.id)
                     and user_states.get(m.from_user.id) == "waiting_task_title")
def receive_task_title(message):
    tg_id = message.from_user.id
    title = message.text.strip() if message.text else ""
    if not title:
        bot.reply_to(message, "❌ العنوان لا يمكن أن يكون فارغاً.")
        return
    user_states[tg_id] = f"waiting_task_desc|{title}"
    bot.reply_to(message, "✏️ أرسل وصف المهمة (أو أرسل /skip للتخطي):")

# ========== إضافة مهمة: استقبال الوصف ==========
@bot.message_handler(func=lambda m: is_admin(m.from_user.id)
                     and isinstance(user_states.get(m.from_user.id, ""), str)
                     and user_states.get(m.from_user.id, "").startswith("waiting_task_desc|"))
def receive_task_desc(message):
    tg_id = message.from_user.id
    title = user_states[tg_id].split("|", 1)[1]
    raw = message.text.strip() if message.text else ""
    desc = "" if raw in ["/skip", "تخطي"] else raw
    user_states[tg_id] = f"waiting_task_reward|{title}|{desc}"
    bot.reply_to(message, "💰 أرسل قيمة المكافأة (نقطة ⭐):")

# ========== إضافة مهمة: استقبال المكافأة وإنشاء المهمة ==========
@bot.message_handler(func=lambda m: is_admin(m.from_user.id)
                     and isinstance(user_states.get(m.from_user.id, ""), str)
                     and user_states.get(m.from_user.id, "").startswith("waiting_task_reward|"))
def receive_task_reward(message):
    tg_id = message.from_user.id
    state = user_states[tg_id]
    _, title, desc = state.split("|", 2)

    raw = message.text.strip() if message.text else ""
    try:
        reward = float(raw)
    except ValueError:
        bot.reply_to(message, "❌ القيمة يجب أن تكون رقماً.")
        return
    if reward <= 0:
        bot.reply_to(message, "❌ أدخل قيمة موجبة فقط.")
        return

    task_id = create_task(title, desc, reward, tg_id)
    log_admin_action(tg_id, "create_task", f"task_id={task_id}, title={title}, reward={reward}")
    user_states.pop(tg_id, None)

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 رجوع لإدارة المهام", callback_data="admin_tasks"))
    bot.reply_to(message,
        f"✅ *تم إنشاء المهمة بنجاح*\n\n"
        f"📌 {escape_markdown(title)}\n"
        f"💰 المكافأة: {reward} نقطة ⭐",
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
    global BOT_USERNAME
    if not BOT_USERNAME:
        try:
            BOT_USERNAME = bot.get_me().username
        except Exception:
            pass
    while True:
        try:
            process_pending_referrals()
        except Exception as e:
            print(f"⚠️ خطأ في referrals_loop: {e}")
        time.sleep(120)

# ======================= تشغيل البوت =======================
if __name__ == "__main__":
    bot.set_my_commands([telebot.types.BotCommand("start", "بدء البوت")])
    threading.Thread(target=referrals_loop, daemon=True).start()
    threading.Thread(target=auto_backup, daemon=True).start()
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
