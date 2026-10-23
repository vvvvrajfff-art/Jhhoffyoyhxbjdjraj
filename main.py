#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LX Hosting PRO v4 - ULTIMATE HOSTING PRO (VPS Edition)
Single-file Telegram bot hosting platform (pyTelegramBotAPI).

    pip install pyTelegramBotAPI requests psutil autopep8
    python3 lx_hosting_pro.py

ARCHITECTURE MAP
  [1] Rule of Three ....... triple(): Plan A -> Plan B -> Plan C, everywhere.
  [2] Five agents (free) .. Code Fixer / Communicator / Load Manager / Security Scanner / Admin Advisor
  [3] Anti-crash .......... bounded upload queue, self-reviving workers, per-bot supervisors in their own
                            process groups, guardian thread, global exception hooks.
  [4] UI .................. colored buttons (Bot API `style`), mixed wide/narrow rows.
  [5] Dual Admin .......... ADMIN_CHAT_IDS: both have ultimate power. Multi-level paginated dashboard.
  [6] Low-RAM mode ....... Docker and optional AI disabled; hosted bots use subprocess supervisors.
  [7] Security Layer ...... AST + text scanners, plain-text bomber / nested-host / scrape ban-lists.

v3.1 CHANGES
  * Aggressive garbage collection: rejected files are never kept; re-uploading a bot token you already host
    terminates the old process/container and DELETES the old folder before the new file is hosted;
    orphan folders / logs / containers are swept automatically.
  * Admin Panel -> "Update Bot Code": send a new .py, the bot overwrites itself and restarts via os.execl
    (with automatic rollback if the new code cannot start).
  * Free hosting disabled; five verified referrals earn one permanent hosting credit.
"""
import os, sys, re, ast, gc, json, time, queue, shutil, signal, sqlite3, logging, hashlib, importlib
import tempfile, threading, functools, subprocess, traceback, secrets
try:
    import resource
except ImportError:  # non-Linux
    resource = None
try:
    import fcntl
except ImportError:  # non-Linux
    fcntl = None
from datetime import datetime, timedelta, timezone
from html import escape
import requests
from flask import Flask, request, jsonify, Response
import telebot
from telebot import types, apihelper

# Low-RAM mode: these flags must exist before optional imports are evaluated.
AI_ENABLED = False
DOCKER_ENABLED = False

# ---- optional FREE libraries (each one has a fallback, so a missing one never crashes the host) ----
try:
    import psutil
except Exception:
    psutil = None
try:
    import autopep8
except Exception:
    autopep8 = None
# AI is disabled by default to reduce RAM use; no g4f client is imported.
G4FClient = None

# Docker is intentionally removed/disabled. Hosted bots use the existing subprocess
# supervisor instead; no Docker SDK import or daemon connection is attempted.
docker = None

# ============================ CONFIGURATION ============================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# --- DUAL ADMIN: BOTH have ultimate power. Add a third, fourth, ... if you want. ---
ADMIN_CHAT_IDS = [
    8432635520,        # primary admin (your numeric Telegram ID)
    8674625051,        # second admin (replace or add more)
]
ADMIN_CHAT_ID = ADMIN_CHAT_IDS[0]        # kept for internal back-compat only

DEBUG_ERRORS_TO_ADMIN = True
DEVELOPER_USERNAME = "lxking99"

# --- Mandatory channels (bot must be an ADMIN in both to check membership) ---
REQUIRED_CHANNEL_1 = "@nasimvaiz"
REQUIRED_CHANNEL_2 = "@Animaxking77"

# Set this to the channel where daily redeem codes will be announced.
REDEEM_CHANNEL = "@lxking99"  # Replace with your channel username

# --- FamGateway (automatic UPI payments) -- https://famgateway.in/docs.php ---
FAM_GATEWAY_API_KEY = os.getenv("FAM_GATEWAY_API", "")
FAM_GATEWAY_BASE = "https://famgateway.in"
PAYMENT_POLL_SECONDS = 4
PAYMENT_POLL_MAX_MIN = 5

# --- Network safety ---
NET_TIMEOUT = 15

# Flask / Telegram webhook configuration. Put your deployed public HTTPS URL here.
# Telegram requires a publicly reachable HTTPS URL for webhook delivery.
WEBHOOK_SECRET = hashlib.sha256(("lx-webhook:" + BOT_TOKEN).encode()).hexdigest()[:40]
WEBHOOK_PATH = "/telegram/webhook"
app = Flask(__name__)
_runtime_init_lock = threading.Lock()
_runtime_initialized = False

# --- Limits & plans ---
FREE_BOT_LIMIT = 1          # Free hosting disabled
PREMIUM_BOT_LIMIT = 10
REFERRAL_BONUS_SLOTS = 1
CURRENCY = "INR"
DEFAULT_PLANS = [("1 Month", 30, 30), ("2 Months", 60, 60), ("6 Months", 180, 100)]
MAX_FILE_MB = 2
AUTO_RESTART = True
MAX_RESTARTS = 5
RESTART_WINDOW_SEC = 600
LIMIT_MEMORY_MB = 256          # RLIMIT_AS cap for subprocess-hosted bots; per-process, not a RAM reservation
LIMIT_FILE_MB = 200
NOTIFY_ADMIN_ON_DEPLOY = True

# --- Storage hygiene / OTA update ---
SWEEP_INTERVAL_SEC = 600       # how often orphan files/containers are swept
SWEEP_MIN_AGE_SEC = 300        # never touch things younger than this (in-flight uploads)
OTA_MAX_MB = 5                 # max size of an uploaded replacement script
OTA_HEALTH_WAIT_SEC = 90       # new code must report healthy within this time or it is rolled back

# --- Agent 3: Load Manager ---
MONITOR_INTERVAL = 20
BOT_RAM_LIMIT_MB = 80           # Free-plan runtime RSS cap; keep host RAM available
SYSTEM_RAM_CRITICAL = 80         # pause the heaviest non-admin bot before Linux OOM pressure
FROZEN_CPU_PCT = 95
FROZEN_STRIKES = 9

# --- Agents 2 & 5: free AI via g4f ---
AI_ENABLED = False              # optional g4f can be memory-heavy on a small VPS; enable only with headroom
AI_TIMEOUT = 25
AI_MODELS = ["gpt-4o-mini", "gpt-4", "llama-3.1-70b"]
AI_USER_COOLDOWN = 6
DAILY_REPORT_HOURS = 24

# --- Upload queue ---
UPLOAD_WORKERS = 1               # one dependency install at a time on a ~1.5 GB server
UPLOAD_QUEUE_MAX = 8             # bounded queue avoids retaining many upload jobs in memory

# --- Docker is disabled for low-RAM hosting; all hosted bots use subprocess mode. ---
DOCKER_ENABLED = False
DOCKER_BASE_IMAGE = "python:3.11-slim"
DOCKER_MEM_PREMIUM = "160m"      # premium gets more than free, but never the whole VPS
DOCKER_CPUS_PREMIUM = 0.5
DOCKER_MEM_FREE_FALLBACK = f"{BOT_RAM_LIMIT_MB}m"
DOCKER_CPUS_FREE_FALLBACK = 0.25
DOCKER_INSTALL_SDK_IF_MISSING = False      # never run pip install at bot startup on a low-memory VPS

STICKER_START = "file_id_here"
STICKER_HOSTED = "file_id_here"
STICKER_PREMIUM = "file_id_here"

JUNK_MESSAGE_TTL = 8
# ========================== END OF CONFIGURATION =======================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOSTED_DIR = os.path.join(BASE_DIR, "hosted_bots")
PRIV_DIR = os.path.join(BASE_DIR, "lx_private")
LOGS_DIR = os.path.join(PRIV_DIR, "logs")
DB_PATH = os.path.join(PRIV_DIR, "lx_hosting.db")
START_TIME = time.time()
SCRIPT_PATH = os.path.abspath(__file__)
OTA_BACKUP_PATH = os.path.join(PRIV_DIR, "main_script.previous.py")
OTA_OK_FLAG = os.path.join(PRIV_DIR, "boot_ok.flag")
OTA_RB_FLAG = os.path.join(PRIV_DIR, "ota_rolled_back.flag")
LOCK_PATH = os.path.join(PRIV_DIR, "lx.lock")

_handlers = [logging.StreamHandler()]
try:
    os.makedirs(PRIV_DIR, exist_ok=True)
    _handlers.append(logging.FileHandler(os.path.join(PRIV_DIR, "lx.log"), encoding="utf-8"))
except OSError:
    pass
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=_handlers)
log = logging.getLogger("lx")
threading.excepthook = lambda a: log.error("Unhandled thread error", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
sys.excepthook = lambda t, v, tb: log.error("Unhandled exception", exc_info=(t, v, tb))

if not BOT_TOKEN:
    log.warning("BOT_TOKEN is empty; Telegram API calls will fail until it is filled in the CONFIGURATION section.")

apihelper.CONNECT_TIMEOUT = NET_TIMEOUT
apihelper.READ_TIMEOUT = NET_TIMEOUT


class _ErrHandler(telebot.ExceptionHandler):
    def handle(self, exception):
        log.error("telebot handler exception: %r", exception)
        return True


bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True, num_threads=8, exception_handler=_ErrHandler())
BOT_USERNAME = ""


# ---- dual-admin helpers (defined EARLY: many things depend on them) ----
def is_admin(uid):
    try:
        return int(uid) in ADMIN_CHAT_IDS
    except (TypeError, ValueError):
        return False


def notify_admins(text, **kw):
    """Fan out to every admin. Never raises, never blocks."""
    for aid in ADMIN_CHAT_IDS:
        try:
            safe_send(aid, text, **kw)
        except Exception:
            log.debug("notify_admins: %s failed", aid)


# ---- button labels ----
BTN_UPLOAD = "📤 Upload File"
BTN_MYBOTS = "📂 My Bots"
BTN_STATUS = "📊 My Bots Status"
BTN_PROFILE = "👤 Profile"
BTN_PREMIUM = "💎 Buy Premium"
BTN_REFER = "🎁 Refer"
BTN_REDEEM = "🎟️ Redeem Code"
BTN_PING = "⚡ Server Ping"
BTN_PRO = "🚀 Pro Features"
BTN_DEV = "👨‍💻 Developer"
BTN_HELP = "🛠 Help & Support"
BTN_ADMIN = "⚙️ Admin Panel"
MYBOTS_TXT = (BTN_MYBOTS, "🤖 My Bots")
HELP_TXT = (BTN_HELP, "🆘 Help", "🆘 Help & Support")

DIV = "━━━━━━━━━━━━━━━━━━"


# ============================ [1] RULE OF THREE ============================
def triple(label, plan_a, plan_b=None, plan_c=None, default=None):
    for tier, fn in (("A", plan_a), ("B", plan_b), ("C", plan_c)):
        if fn is None:
            continue
        try:
            res = fn()
            if tier != "A":
                log.info("[Rule of Three] %s recovered with plan %s", label, tier)
            return res, tier
        except Exception as e:
            log.warning("[Rule of Three] %s: plan %s failed: %s", label, tier, e)
    log_event("error", detail=f"all plans failed: {label}")
    return default, "FAIL"


def run_with_timeout(fn, timeout):
    box = {}

    def w():
        try:
            box["r"] = fn()
        except BaseException as e:
            box["e"] = e
    t = threading.Thread(target=w, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"timed out after {timeout}s")
    if "e" in box:
        raise box["e"]
    return box.get("r")


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def esc(x):
    return escape(str(x if x is not None else ""))


def fmt_dur(sec):
    sec = int(max(0, sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return (f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}m" + ("" if d or h else f" {s}s")


def pbar(pct, width=10):
    pct = max(0, min(100, pct or 0))
    n = round(width * pct / 100)
    return "▰" * n + "▱" * (width - n)


class RawInline(types.InlineKeyboardMarkup):
    def __init__(self, d):
        super().__init__()
        self.d = d

    def to_json(self):
        return json.dumps(self.d, ensure_ascii=False)

    def to_dict(self):
        return self.d


class RawReply(types.ReplyKeyboardMarkup):
    def __init__(self, d):
        super().__init__()
        self.d = d

    def to_json(self):
        return json.dumps(self.d, ensure_ascii=False)

    def to_dict(self):
        return self.d


def btn(text, style=None, cb=None, url=None):
    d = {"text": text}
    if cb:
        d["callback_data"] = cb
    if url:
        d["url"] = url
    if style:
        d["style"] = style
    return d


def inline(rws):
    return RawInline({"inline_keyboard": rws})


def main_menu(chat_id=None):
    kb = [
        [btn(BTN_UPLOAD, "success")],
        [btn(BTN_MYBOTS, "primary"), btn(BTN_STATUS, "primary")],
        [btn(BTN_PROFILE, "primary"), btn(BTN_PREMIUM, "success"), btn(BTN_REFER, "primary")],
        [btn(BTN_REDEEM, "success")],
        [btn(BTN_PING, "success"), btn(BTN_PRO, "primary")],
        [btn(BTN_HELP, "primary")],
        [btn(BTN_DEV, "primary")],
    ]
    if chat_id is not None and is_admin(chat_id):
        kb.append([btn(BTN_ADMIN, "danger")])
    return RawReply({"keyboard": kb, "resize_keyboard": True, "is_persistent": True,
                     "input_field_placeholder": "Choose an option 👇"})


def safe(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        try:
            return fn(*a, **k)
        except telebot.apihelper.ApiTelegramException as e:
            if "not modified" not in str(e):
                log.warning("Telegram API error in %s: %s", fn.__name__, e)
        except Exception:
            log.exception("Error in %s", fn.__name__)
            log_event("error", detail=f"handler {fn.__name__}")
            try:
                a0 = a[0]
                if isinstance(a0, types.CallbackQuery):
                    bot.answer_callback_query(a0.id, "⚠️ Something went wrong. Try again.")
                elif isinstance(a0, types.Message):
                    bot.send_message(a0.chat.id, "⚠️ Something went wrong. Please try again.")
            except Exception:
                pass
    return w


def safe_send(chat_id, text, **kw):
    def a():
        return bot.send_message(chat_id, text, **kw)

    def b():
        kw2 = dict(kw)
        kw2["parse_mode"] = None
        return bot.send_message(chat_id, re.sub(r"<[^>]+>", "", text)[:4000], **kw2)

    def c():
        return bot.send_message(chat_id, "ℹ️ You have a new notification. Open the bot to see details.")
    res, _ = triple("safe_send", a, b, c)
    return res


def auto_delete(chat_id, message_id, delay=None):
    def _del():
        try:
            bot.delete_message(chat_id, message_id)
        except Exception as e:
            log.debug("auto_delete: %s", e)
    threading.Timer(JUNK_MESSAGE_TTL if delay is None else delay, _del).start()


def send_sticker_safe(chat_id, file_id):
    if not file_id or file_id == "file_id_here":
        return
    try:
        bot.send_sticker(chat_id, file_id)
    except Exception as e:
        log.debug("send_sticker_safe: %s", e)


def animate(chat_id, mid, frames, delay=1.1, final_markup=None, is_caption=False):
    for i, f in enumerate(frames):
        mk = final_markup if i == len(frames) - 1 else None
        try:
            if is_caption:
                bot.edit_message_caption(caption=f, chat_id=chat_id, message_id=mid, reply_markup=mk)
            else:
                bot.edit_message_text(f, chat_id, mid, reply_markup=mk)
        except Exception as e:
            log.debug("animate edit failed: %s", e)
        if i < len(frames) - 1:
            time.sleep(delay)


# ------------------------------- database ------------------------------
_db_lock = threading.RLock()


def rows(sql, args=()):
    with _db_lock:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()


def run(sql, args=()):
    with _db_lock:
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            cur = con.execute(sql, args)
            con.commit()
            return cur.lastrowid if cur.lastrowid else cur.rowcount
        finally:
            con.close()


def init_db():
    for d in (HOSTED_DIR, PRIV_DIR, LOGS_DIR):
        os.makedirs(d, exist_ok=True)
    with _db_lock:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined_at TEXT,
            confirmed INTEGER DEFAULT 0, extra_slots INTEGER DEFAULT 0, lifetime_free_used INTEGER DEFAULT 0,
            referral_credits INTEGER DEFAULT 0, referral_milestones INTEGER DEFAULT 0,
            premium_until TEXT, banned INTEGER DEFAULT 0, referred_by INTEGER);
        CREATE TABLE IF NOT EXISTS referrals(
            referred_id INTEGER PRIMARY KEY, referrer_id INTEGER, created TEXT, completed INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS bots(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT,
            status TEXT DEFAULT 'stopped', autorestart INTEGER DEFAULT 1, created TEXT,
            runtime TEXT DEFAULT 'subprocess',       -- 'subprocess' or 'docker'
            public_id TEXT UNIQUE,
            UNIQUE(user_id, name));
        CREATE TABLE IF NOT EXISTS plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, days INTEGER, price INTEGER);
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, order_id TEXT UNIQUE, plan_id INTEGER,
            amount REAL, status TEXT DEFAULT 'pending', created TEXT, paid_at TEXT, utr TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS redeem_codes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE COLLATE NOCASE,
            credits INTEGER NOT NULL CHECK(credits > 0),
            used_by INTEGER,
            used_username TEXT,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            redeemed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_redeem_used_by ON redeem_codes(used_by);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, user_id INTEGER, bot_id INTEGER, detail TEXT);
        """)
        for ddl in ("ALTER TABLE users ADD COLUMN lifetime_free_used INTEGER DEFAULT 0",
                    "ALTER TABLE users ADD COLUMN referral_credits INTEGER DEFAULT 0",
                    "ALTER TABLE users ADD COLUMN referral_milestones INTEGER DEFAULT 0",
                    "ALTER TABLE referrals ADD COLUMN completed INTEGER DEFAULT 0",
                    "ALTER TABLE bots ADD COLUMN tg_username TEXT",
                    "ALTER TABLE bots ADD COLUMN runtime TEXT DEFAULT 'subprocess'",
                    "ALTER TABLE bots ADD COLUMN token_hash TEXT",
                    "ALTER TABLE bots ADD COLUMN public_id TEXT"):
            try:
                con.execute(ddl)
            except sqlite3.OperationalError:
                pass
        if not con.execute("SELECT 1 FROM plans LIMIT 1").fetchone():
            con.executemany("INSERT INTO plans(label,days,price) VALUES(?,?,?)", DEFAULT_PLANS)
        for aid in ADMIN_CHAT_IDS:
            con.execute("INSERT OR IGNORE INTO users(user_id,joined_at,confirmed) VALUES(?,?,1)",
                        (aid, utcnow().isoformat()))
        con.commit()
        # Backfill stable, unguessable public IDs for bots created by older versions.
        missing_ids = con.execute("SELECT id FROM bots WHERE public_id IS NULL OR public_id=''").fetchall()
        for (legacy_id,) in missing_ids:
            con.execute("UPDATE bots SET public_id=? WHERE id=?", (secrets.token_urlsafe(18), legacy_id))
        con.commit()
        con.close()


def ensure_public_bot_id(bot_id):
    r = rows("SELECT public_id FROM bots WHERE id=?", (bot_id,))
    if not r:
        return None
    if r[0]["public_id"]:
        return r[0]["public_id"]
    public_id = secrets.token_urlsafe(18)
    run("UPDATE bots SET public_id=? WHERE id=? AND (public_id IS NULL OR public_id='')", (public_id, bot_id))
    r = rows("SELECT public_id FROM bots WHERE id=?", (bot_id,))
    return r[0]["public_id"] if r else public_id


def get_setting(key, default=None):
    r = rows("SELECT value FROM settings WHERE key=?", (key,))
    return r[0]["value"] if r else default


def set_setting(key, value):
    run("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))


def log_event(kind, uid=None, bot_id=None, detail=""):
    try:
        run("INSERT INTO events(ts,kind,user_id,bot_id,detail) VALUES(?,?,?,?,?)",
            (utcnow().isoformat(timespec="seconds"), kind, uid, bot_id, str(detail)[:300]))
    except Exception:
        pass


def get_user(uid):
    r = rows("SELECT * FROM users WHERE user_id=?", (uid,))
    return r[0] if r else None


def is_premium(uid):
    u = get_user(uid)
    try:
        return bool(u and u["premium_until"] and datetime.fromisoformat(u["premium_until"]) > utcnow())
    except ValueError:
        return False


def premium_cap(uid):
    u = get_user(uid)
    return PREMIUM_BOT_LIMIT + (u["extra_slots"] if u else 0)


def free_credit_total(u):
    return 0


def free_remaining(uid):
    u = get_user(uid)
    return int(u["referral_credits"] or 0) if u else 0


CREDIT_GONE_MSG = ("🚫 Free hosting is disabled. Buy 💎 Premium to host up to 10 bots, "
                   "or complete 5 verified referrals to earn 1 permanent hosting credit.")


def claim_free_credit(uid):
    return bool(run("UPDATE users SET referral_credits=referral_credits-1 "
                    "WHERE user_id=? AND COALESCE(referral_credits,0)>0", (uid,)))


def release_free_credit(uid):
    run("UPDATE users SET referral_credits=COALESCE(referral_credits,0)+1 WHERE user_id=?", (uid,))


def bot_count(uid):
    return rows("SELECT COUNT(*) c FROM bots WHERE user_id=?", (uid,))[0]["c"]


def can_create_new_bot(uid):
    if is_admin(uid):
        return True
    if is_premium(uid):
        return bot_count(uid) < premium_cap(uid)
    return free_remaining(uid) > 0


def award_verified_referral(uid):
    ref = rows("SELECT referrer_id FROM referrals WHERE referred_id=?", (uid,))
    if not ref:
        return
    referrer = int(ref[0]["referrer_id"])
    run("UPDATE referrals SET completed=1 WHERE referred_id=? AND referrer_id=?", (uid, referrer))
    verified = rows("SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND completed=1", (referrer,))[0]["c"]
    milestones = verified // 5
    u = get_user(referrer)
    old = int(u["referral_milestones"] or 0) if u else 0
    if milestones > old:
        earned = milestones - old
        run("UPDATE users SET referral_credits=COALESCE(referral_credits,0)+?, referral_milestones=? WHERE user_id=?",
            (earned, milestones, referrer))
        safe_send(referrer, f"🎉 <b>Referral reward unlocked!</b>\\nYou earned {earned} permanent hosting credit(s).\\n"
                            f"Available credits: <b>{free_remaining(referrer)}</b>.")



def register(user, ref=None):
    if get_user(user.id) is not None:
        run("UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (user.username, user.first_name, user.id))
        return False
    ref_ok = bool(ref and ref != user.id and get_user(ref))
    run("INSERT INTO users(user_id,username,first_name,joined_at,referred_by) VALUES(?,?,?,?,?)",
        (user.id, user.username, user.first_name, utcnow().isoformat(), ref if ref_ok else None))
    if ref_ok:
        run("INSERT OR IGNORE INTO referrals(referred_id,referrer_id,created) VALUES(?,?,?)",
            (user.id, ref, utcnow().isoformat()))
    return True


def blocked(uid):
    if is_admin(uid):
        return None
    u = get_user(uid)
    if not u:
        return "Please send /start first."
    if u["banned"]:
        return f"🚫 Your account is suspended. Contact @{DEVELOPER_USERNAME}."
    if not u["confirmed"]:
        return "Please send /start and confirm both channel joins first."
    return None


# ------------------------- real channel join check ----------------------
def is_member(channel, uid):
    try:
        cm = bot.get_chat_member(channel, uid)
        return cm.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning("get_chat_member(%s,%s) failed: %s", channel, uid, e)
        return False


def missing_channels(uid):
    return [c for c in (REQUIRED_CHANNEL_1, REQUIRED_CHANNEL_2) if not is_member(c, uid)]


def join_markup(missing=None):
    chans = missing if missing else (REQUIRED_CHANNEL_1, REQUIRED_CHANNEL_2)
    rws = [[btn(f"📢 Join {c}", "primary", url=f"https://t.me/{c.lstrip('@')}")] for c in chans]
    rws.append([btn("✅ I've Joined Both", "success", cb="joined")])
    return inline(rws)


# ------------------------- folders / execution helpers ------------------
def log_path(bot_id):
    return os.path.join(LOGS_DIR, f"{bot_id}.log")


def user_root(uid):
    d = os.path.join(HOSTED_DIR, str(uid))
    os.makedirs(d, exist_ok=True)
    return d


def bot_dir(uid, name):
    return os.path.join(user_root(uid), name)


def limiter():
    if resource is None:
        return None

    def f():
        def lim(res, mb):
            if mb and mb > 0:
                try:
                    resource.setrlimit(res, (mb * 1024 * 1024, mb * 1024 * 1024))
                except Exception:
                    pass
        lim(resource.RLIMIT_AS, LIMIT_MEMORY_MB)
        lim(resource.RLIMIT_FSIZE, LIMIT_FILE_MB)
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except Exception:
            pass
    return f


def venv_python(d):
    return os.path.join(d, ".venv", "bin", "python")


def python_candidates(d):
    c = []
    if os.path.exists(venv_python(d)):
        c.append(venv_python(d))
    c.append(sys.executable)
    return c


def bot_env(d):
    return {"PATH": f"{os.path.join(d, '.venv', 'bin')}:{os.path.dirname(sys.executable)}:/usr/local/bin:/usr/bin:/bin",
            "HOME": d, "TMPDIR": os.path.join(d, ".tmp"), "PYTHONUNBUFFERED": "1", "LANG": "C.UTF-8",
            "PYTHONPATH": os.path.join(d, "libs")}


def prepare_dir(uid, name):
    d = bot_dir(uid, name)
    for sub in ("", ".tmp", "libs"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d


def write_user_file(d, filename, data):
    dest = os.path.join(d, filename)

    def a():
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".up_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def b():
        with open(dest, "wb") as f:
            f.write(data)

    def c():
        fd, tmp = tempfile.mkstemp(prefix="lx_")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        shutil.copyfile(tmp, dest)
        os.remove(tmp)
    _, tier = triple(f"write {filename}", a, b, c)
    if tier == "FAIL":
        raise OSError(f"could not write {filename}")


def _py_ok(py, d, *args):
    try:
        r = subprocess.run([py, *(args or ("-c", "import sys"))], cwd=d, capture_output=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def ensure_venv(d):
    v, marker = venv_python(d), os.path.join(d, ".venv_failed")
    try:
        if os.path.exists(v) and _py_ok(v, d, "-m", "pip", "--version"):
            return True
        if os.path.exists(marker):
            return False
        shutil.rmtree(os.path.join(d, ".venv"), ignore_errors=True)
        r = subprocess.run([sys.executable, "-m", "venv", os.path.join(d, ".venv")], cwd=d,
                           capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and _py_ok(v, d, "-m", "pip", "--version"):
            return True
        log.warning("venv unusable, using fallback: %s", (r.stderr or "")[-300:])
    except Exception:
        log.exception("venv creation error, using fallback (system python + ./libs)")
    shutil.rmtree(os.path.join(d, ".venv"), ignore_errors=True)
    try:
        open(marker, "w").close()
    except OSError:
        pass
    return False


# --------------------- dependency detection / install ------------------
PIP_MAP = {
    "telebot": "pyTelegramBotAPI", "telegram": "python-telegram-bot", "cv2": "opencv-python-headless",
    "PIL": "Pillow", "bs4": "beautifulsoup4", "yaml": "PyYAML", "dotenv": "python-dotenv",
    "sklearn": "scikit-learn", "skimage": "scikit-image", "Crypto": "pycryptodome",
    "Cryptodome": "pycryptodomex", "dateutil": "python-dateutil", "jwt": "PyJWT",
    "magic": "python-magic", "serial": "pyserial", "git": "GitPython", "docx": "python-docx",
    "pptx": "python-pptx", "fitz": "PyMuPDF", "attr": "attrs", "OpenSSL": "pyOpenSSL",
    "psycopg2": "psycopg2-binary", "tgcrypto": "TgCrypto", "flask": "Flask", "jinja2": "Jinja2",
    "bson": "pymongo", "speech_recognition": "SpeechRecognition", "deep_translator": "deep-translator",
    "pymysql": "PyMySQL", "mysql": "mysql-connector-python", "apscheduler": "APScheduler",
    "faker": "Faker", "discord": "discord.py", "slack_bolt": "slack-bolt", "slack_sdk": "slack-sdk",
    "googletrans": "googletrans==4.0.0rc1", "duckduckgo_search": "duckduckgo-search",
    "telegraph": "telegraph", "google": "google-api-python-client",
}
PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,60}(==[A-Za-z0-9.]+(rc\d+)?)?$")
BOT_TOKEN_RE = re.compile(r"\b\d{8,10}:[a-zA-Z0-9_-]{35}\b")

MISSING_MODULE_PATTERNS = [
    re.compile(r"ModuleNotFoundError: No module named '([\w.]+)'"),
    re.compile(r"ImportError: No module named '?([\w.]+)'?"),
    re.compile(r"ImportError: cannot import name '[\w.]+' from '([\w.]+)'"),
]


def extract_missing_module(log_text):
    found = None
    for pat in MISSING_MODULE_PATTERNS:
        for m in pat.finditer(log_text):
            found = m.group(1)
    return found.split(".")[0] if found else None


def lookup_username(src):
    try:
        m = BOT_TOKEN_RE.search(src)
        if not m:
            return "-"
        r = requests.get(f"https://api.telegram.org/bot{m.group(0)}/getMe", timeout=NET_TIMEOUT)
        data = r.json()
        if data.get("ok"):
            uname = data["result"].get("username")
            return f"@{uname}" if uname else "-"
        return "-"
    except Exception:
        log.warning("lookup_username failed", exc_info=True)
        return None


def resolve_and_store_username(bot_id, uid, name):
    try:
        with open(os.path.join(bot_dir(uid, name), name + ".py"), "r", encoding="utf-8", errors="replace") as f:
            res = lookup_username(f.read())
        if res is not None:
            run("UPDATE bots SET tg_username=? WHERE id=?", (res, bot_id))
        return res
    except Exception:
        return None


def backfill_usernames():
    for b in rows("SELECT id,user_id,name FROM bots WHERE tg_username IS NULL"):
        resolve_and_store_username(b["id"], b["user_id"], b["name"])
        time.sleep(0.3)


STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


def extract_imports(src):
    mods = set()
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return mods
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mods.add(n.module.split(".")[0])
    return mods


def packages_for(src, d):
    local = set()
    try:
        for f in os.listdir(d):
            local.add(f[:-3] if f.endswith(".py") else f)
    except OSError:
        pass
    pkgs = []
    for m in sorted(extract_imports(src)):
        if m in STDLIB or m in local:
            continue
        p = PIP_MAP.get(m, m)
        if PKG_RE.match(p):
            pkgs.append(p)
    return pkgs


def _run_pip(py, d, pkg, target=None):
    cmd = [py, "-m", "pip", "install", "--prefer-binary", "--no-input", "--disable-pip-version-check", "-q"]
    if target:
        cmd += ["--target", target]
    cmd.append(pkg)
    r = subprocess.run(cmd, cwd=d, env=bot_env(d), capture_output=True, text=True, timeout=600)
    if r.returncode != 0 and "No module named pip" in (r.stderr or ""):
        try:
            subprocess.run([py, "-m", "ensurepip", "--user"], cwd=d, capture_output=True, timeout=180)
            r = subprocess.run(cmd, cwd=d, env=bot_env(d), capture_output=True, text=True, timeout=600)
        except Exception:
            pass
    return r


def pip_install(d, pkgs, progress_cb=None):
    ok, bad = [], []
    libs = os.path.join(d, "libs")
    try:
        os.makedirs(libs, exist_ok=True)
    except OSError:
        pass
    try:
        use_venv = ensure_venv(d)
    except Exception:
        log.exception("ensure_venv crashed")
        use_venv = False
    for i, p in enumerate(pkgs, 1):
        if progress_cb:
            try:
                progress_cb(i, len(pkgs), p)
            except Exception:
                pass
        attempts = ([(venv_python(d), None)] if use_venv else []) + [(sys.executable, libs)]
        done = False
        for py, target in attempts:
            try:
                r = _run_pip(py, d, p, target)
                if r.returncode == 0:
                    done = True
                    break
                log.warning("pip (%s) failed for %s: %s", "venv" if target is None else "--target", p,
                            (r.stderr or "")[-300:])
            except Exception:
                log.exception("pip error for %s", p)
        (ok if done else bad).append(p)
    return ok, bad


# ===================== [2] AGENT 4 - SECURITY SCANNER =====================
# The ORIGINAL instant AST scanner is kept intact below. Agent 4 layers extra checks
# on top of it and adds a text-level Plan B plus a fail-closed Plan C.
SENSITIVE_FILE_RE = re.compile(
    r"/etc/(passwd|shadow|gshadow|sudoers)\b|/proc/[^\s'\"]*environ|"
    r"\.ssh/(id_|authorized_keys)|\.bash_history")
SWEEP_ROOT_RE = re.compile(r"^/$|^/(root|home)(/|$)")
WALK_NAMES = {"os.walk", "os.fwalk", "os.scandir", "os.listdir", "glob.glob", "glob.iglob"}
WALK_ATTRS = {"walk", "fwalk", "scandir", "listdir", "glob", "iglob", "rglob", "iterdir"}
TOKEN_RE_HINTS = (
    re.compile(r"(\\d|\[0-9\])(\{\d+(,\d+)?\}|\+):"),
    re.compile(r":\s*(AA)?\[[^\]]*(a-z|A-Z|\\w)[^\]]*\]\{3\d\}"),
)
HOST_PATH_RE = re.compile(r"lx_private|lx_hosting|hosted_bots" +
                          (("|" + re.escape(BASE_DIR)) if BASE_DIR else ""))
BLOCKED_CALLS = {"os.remove", "os.unlink", "os.rmdir", "os.removedirs", "shutil.rmtree",
                 "eval", "exec", "builtins.eval", "builtins.exec"}

# ---- V3: plain-text ban lists for bombers, nested hosts, scrapers, flood loops ----
BOMBER_RE = re.compile(
    r"\b(sms|call)[\s_\-]?bomb(er|ing)?\b|"
    r"\bbomb(er|ing)\b|"
    r"\btextbelt|fast2sms|msg91|bulk[\s_\-]?sms|sms[\s_\-]?gateway|"
    r"twilio[^,\n]{0,40}(bulk|spam|mass|flood)|"
    r"\bspam[\s_\-]?(api|call|request|sms|loop)|"
    r"\bapi[\s_\-]?flood\b|"
    r"\bflood(er|ing)?\b\s*(api|call|sms|request)|"
    r"\b(dos|ddos)\s+(attack|tool|script)",
    re.I)
NESTED_HOST_RE = re.compile(
    r"\blx[\s_\-]?hosting\b|"
    r"\bhosted[\s_\-]?bots\b|"
    r"\bultimate[\s_\-]?hosting\b|"
    r"\b(bot|code)[\s_\-]?hosting[\s_\-]?platform\b|"
    r"\bhost[\s_\-]?other[\s_\-]?(bots|users)\b",
    re.I)
SCRAPE_RE = re.compile(
    r"\bget_chat_members\b|getChatMembers|iter_chat_members|"
    r"\bscrape\s+(phone|number|contact)|"
    r"contacts?\s+of\s+(all|every)\s+(user|member)|"
    r"\bphone[\s_\-]?number[\s_\-]?(list|dump|scrape)",
    re.I)
FLOOD_RE = re.compile(
    r"for\s+\w+\s+in\s+range\(\s*\d{3,}\s*\)\s*:\s*[\s\S]{0,200}?requests?\.(get|post|put|delete)",
    re.M)
MASS_WORKERS_RE = re.compile(r"ThreadPoolExecutor\([^)]*max_workers\s*=\s*(\d{3,})", re.I)


def analyze_source(src):
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError) as e:
        return False, f"File is not valid Python ({str(e)[:80]})"

    alias = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    alias[a.asname] = a.name
                else:
                    alias[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            for a in n.names:
                alias[a.asname or a.name] = f"{n.module}.{a.name}"

    def dotted(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(alias.get(node.id, node.id))
            return ".".join(reversed(parts))
        return None

    reasons = set()
    has_walk = False
    has_sweep_root = False
    has_token_regex = False
    has_bomber_text = False
    has_nested_host_text = False
    has_scrape_text = False

    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            s = n.value
            if SENSITIVE_FILE_RE.search(s):
                reasons.add("reads sensitive system files (/etc/passwd, /proc environ, SSH keys)")
            if SWEEP_ROOT_RE.match(s.strip()):
                has_sweep_root = True
            if any(p.search(s) for p in TOKEN_RE_HINTS):
                has_token_regex = True
            if HOST_PATH_RE.search(s):
                reasons.add("references the hosting platform's own files")
            if BOMBER_RE.search(s):
                has_bomber_text = True
            if NESTED_HOST_RE.search(s):
                has_nested_host_text = True
            if SCRAPE_RE.search(s):
                has_scrape_text = True
        if not isinstance(n, ast.Call):
            continue
        name = dotted(n.func) or ""
        if name in BLOCKED_CALLS:
            reasons.add(f"{name}() is not allowed")
        if name in WALK_NAMES or (isinstance(n.func, ast.Attribute) and n.func.attr in WALK_ATTRS):
            has_walk = True

    if has_walk and has_sweep_root:
        reasons.add("scans server directories (/, /root, /home)")
    if has_token_regex and (has_walk or has_sweep_root):
        reasons.add("searches files for Telegram bot tokens")
    if has_bomber_text:
        reasons.add("SMS/call bomber code")
    if has_nested_host_text:
        reasons.add("nested hosting-bot platform")
    if has_scrape_text:
        reasons.add("phone/contact scraping")
    return (not reasons), "; ".join(sorted(reasons))


# --- Agent 4 extra layer ---
DANGEROUS_CMD_RE = re.compile(
    r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f|rm\s+-[a-zA-Z]*f[a-zA-Z]*r|:\(\)\s*\{|mkfs\.|\bdd\s+if=|/dev/(sd|nvme)|"
    r"chmod\s+-R\s+777\s+/|(curl|wget)[^|\n]*\|\s*(ba)?sh|\bnc\s+-e\b|bash\s+-i\s+>&|\b(pkill|killall)\b")
EXTRA_BLOCKED_CALLS = {"os.kill": "os.kill() could attack other processes",
                       "os.killpg": "os.killpg() could attack other processes",
                       "pty" + ".spawn": "blocked dangerous process-launch call",
                       "signal.pthread_kill": "signal.pthread_kill() is not allowed",
                       "__import__": "__import__() hides imports",
                       "builtins.__import__": "__import__() hides imports",
                       "compile": "compile() allows dynamic code execution"}


def extra_scan(src):
    """Additional AST checks (reverse shells, destructive shell commands, process attacks, hidden imports,
    flood loops, mass thread pools, bomber patterns, nested hosts, scrapers)."""
    tree = ast.parse(src)
    alias = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                alias[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            for a in n.names:
                alias[a.asname or a.name] = f"{n.module}.{a.name}"

    def dotted(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(alias.get(node.id, node.id))
            return ".".join(reversed(parts))
        return None

    reasons = set()
    uses_socket = "socket" in alias.values() or any(v.startswith("socket.") for v in alias.values())
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            s = n.value
            if DANGEROUS_CMD_RE.search(s):
                reasons.add("contains destructive or remote-shell shell commands")
            if BOMBER_RE.search(s):
                reasons.add("SMS/call bomber code")
            if NESTED_HOST_RE.search(s):
                reasons.add("nested hosting-bot platform")
            if SCRAPE_RE.search(s):
                reasons.add("phone/contact scraping")
            if FLOOD_RE.search(s):
                reasons.add("mass request flood loop")
        if isinstance(n, ast.Call):
            name = dotted(n.func) or ""
            if name in EXTRA_BLOCKED_CALLS:
                reasons.add(EXTRA_BLOCKED_CALLS[name])
            if name == "os.dup2" and uses_socket:
                reasons.add("socket + os.dup2() looks like a reverse shell")
            if name == "concurrent.futures.ThreadPoolExecutor":
                for kw in n.keywords:
                    if kw.arg == "max_workers" and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, int) and kw.value.value >= 300:
                        reasons.add("thread pool with excessive workers (flood pattern)")
    return (not reasons), "; ".join(sorted(reasons))


def text_scan(src):
    """Plan B: text-level scan that works even when the file can't be parsed."""
    reasons = set()
    if SENSITIVE_FILE_RE.search(src):
        reasons.add("reads sensitive system files (/etc/passwd, /proc environ, SSH keys)")
    if HOST_PATH_RE.search(src):
        reasons.add("references the hosting platform's own files")
    if DANGEROUS_CMD_RE.search(src):
        reasons.add("contains destructive or remote-shell shell commands")
    if re.search(r"\b(eval|exec)\s*\(", src):
        reasons.add("eval()/exec() is not allowed")
    if re.search(r"\b(os\.(remove|unlink|rmdir|removedirs)|shutil\.rmtree)\s*\(", src):
        reasons.add("file deletion calls are not allowed")
    if any(p.search(src) for p in TOKEN_RE_HINTS) and re.search(r"os\.walk|glob|listdir|scandir", src):
        reasons.add("searches files for Telegram bot tokens")
    if BOMBER_RE.search(src):
        reasons.add("SMS/call bomber code")
    if NESTED_HOST_RE.search(src):
        reasons.add("nested hosting-bot platform")
    if SCRAPE_RE.search(src):
        reasons.add("phone/contact scraping")
    if FLOOD_RE.search(src):
        reasons.add("mass request flood loop")
    return (not reasons), "; ".join(sorted(reasons))


def agent4_scan(src):
    """Agent 4. Plan A: AST + extras. Plan B: text scan. Plan C: fail closed."""
    def a():
        ok, reason = analyze_source(src)
        if not ok and reason.startswith("File is not valid Python"):
            tok, treason = text_scan(src)
            if not tok:
                return False, treason, False
            return False, reason, True
        if ok:
            ok, reason = extra_scan(src)
        return ok, reason, False

    def b():
        ok, reason = text_scan(src)
        return ok, reason, False

    def c():
        return False, "security scanner temporarily unavailable, please retry in a minute", False
    res, _ = triple("agent4_scan", a, b, c, default=(False, "security scanner failed", False))
    return res


# ======================== [2] AGENT 1 - CODE FIXER ========================
_SMART = {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u2013": "-", "\u2014": "-",
          "\u00a0": " ", "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": ""}


def _parses(s):
    try:
        ast.parse(s)
        return True
    except (SyntaxError, ValueError):
        return False


def _fx_newlines(s):
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _fx_fences(s):
    return "\n".join(l for l in s.split("\n") if not l.strip().startswith("```"))


def _fx_tabs(s):
    return re.sub(r"^\t+", lambda m: "    " * len(m.group()), s, flags=re.M)


def _fx_invisible(s):
    return "".join(_SMART.get(ch, ch) if ch in "\u00a0\u200b\u200c\u200d\ufeff" else ch for ch in s)


def _fx_smartquotes(s):
    return "".join(_SMART.get(ch, ch) for ch in s)


def _fx_py2_print(s):
    return re.sub(r"^(\s*)print\s+(?!\()(.+?)\s*$", r"\1print(\2)", s, flags=re.M)


def _fx_py2_except(s):
    return re.sub(r"except\s+([\w.]+)\s*,\s*(\w+)\s*:", r"except \1 as \2:", s)


def _fx_autopep8(s):
    return autopep8.fix_code(s, options={"aggressive": 2})


def _fix_pipeline(src, use_autopep8):
    if _parses(src):
        return src, []
    stages = [("normalised line endings", _fx_newlines), ("removed markdown code fences", _fx_fences),
              ("converted tabs to spaces", _fx_tabs), ("removed invisible characters", _fx_invisible),
              ("fixed Python-2 print statements", _fx_py2_print), ("fixed Python-2 except syntax", _fx_py2_except),
              ("replaced smart quotes", _fx_smartquotes)]
    if use_autopep8:
        stages.append(("autopep8 formatting", _fx_autopep8))
    cur, notes = src, []
    for label, fn in stages:
        try:
            new = fn(cur)
        except Exception:
            continue
        if new != cur:
            cur = new
            notes.append(label)
            if _parses(cur):
                return cur, notes
    return src, []


def agent1_fix(src):
    def a():
        if autopep8 is None:
            raise RuntimeError("autopep8 not installed")
        return _fix_pipeline(src, True)

    def b():
        return _fix_pipeline(src, False)

    def c():
        return src, []
    res, _ = triple("agent1_fix", a, b, c, default=(src, []))
    return res


# ===================== [2] AGENT 2 - THE COMMUNICATOR =====================
AI_SYSTEM = ("You are LX Assistant inside a Telegram bot-hosting service. Help users with Python Telegram bots "
             "(pyTelegramBotAPI), hosting errors and Python in general. Be friendly, give concrete steps, answer in "
             "under 120 words, plain text only. Never ask for tokens or passwords.")
_ai_sem = threading.BoundedSemaphore(3)


def _sanitize(text, n=1500):
    return BOT_TOKEN_RE.sub("<token>", str(text or ""))[-n:]


def _g4f_call(messages):
    client = G4FClient()
    last = None
    for model in AI_MODELS:
        try:
            r = client.chat.completions.create(model=model, messages=messages)
            txt = (r.choices[0].message.content or "").strip()
            if txt:
                return txt
        except Exception as e:
            last = e
    raise RuntimeError(f"all g4f models failed: {last}")


def ai_chat(prompt, system=AI_SYSTEM):
    if not AI_ENABLED or G4FClient is None:
        raise RuntimeError("AI disabled or g4f not installed")
    if not _ai_sem.acquire(blocking=False):
        raise RuntimeError("AI busy")

    def call():
        try:
            return _g4f_call([{"role": "system", "content": system}, {"role": "user", "content": prompt}])
        finally:
            _ai_sem.release()
    return run_with_timeout(call, AI_TIMEOUT)


ERROR_RULES = [
    (r"ModuleNotFoundError|No module named|cannot import name",
     "📦 Your code uses a library that isn't installed. The Auto-Fix Assistant tries to install it automatically; "
     "if it keeps failing, check the spelling of the import."),
    (r"IndentationError|TabError",
     "↔️ Python found inconsistent indentation. Use 4 spaces everywhere (don't mix tabs and spaces)."),
    (r"SyntaxError",
     "✍️ There's a typo in the code (a missing bracket, quote or colon). Check the line number shown below."),
    (r"InvalidToken|Unauthorized|\b401\b|Error code: 404|Token must|token.*invalid",
     "🔑 Telegram rejected your bot token. Create a fresh token in @BotFather and put it in your file."),
    (r"\b409\b|terminated by other getUpdates|Conflict",
     "⚔️ The same bot token is already running somewhere else. Stop the other copy so only one runs."),
    (r"ConnectionError|ReadTimeout|ConnectTimeout|Max retries|Temporary failure|NameResolution",
     "🌐 A temporary network problem occurred. The bot will retry by itself."),
    (r"MemoryError|Cannot allocate memory|Killed",
     "🧠 The bot ran out of memory. Avoid loading huge files or unbounded lists into RAM."),
    (r"FileNotFoundError|No such file",
     "📁 The code opens a file that doesn't exist on the server. Only your single .py file is uploaded."),
    (r"PermissionError",
     "🔒 The code tried something it isn't allowed to do on this server."),
    (r"database is locked|sqlite3\.OperationalError",
     "🗄 The SQLite database was busy. Open the connection with a timeout and close it after each use."),
    (r"NameError|AttributeError|TypeError|KeyError|ValueError|IndexError|ZeroDivisionError",
     "🐛 A bug in the code raised an error while running. The last lines of the log show exactly which line."),
]
GENERIC_EXPLAIN = "🤔 Something unexpected stopped your bot. Open 📜 Logs for details or contact support."


def agent2_explain(error_text):
    err = _sanitize(error_text)

    def a():
        for pat, msg in ERROR_RULES:
            if re.search(pat, err, re.I):
                return msg
        raise LookupError("no rule matched")

    def b():
        txt = ai_chat("Explain this Python/Telegram-bot error to a beginner and suggest one fix:\n" + err)
        return "🤖 " + esc(txt[:600])

    def c():
        return GENERIC_EXPLAIN
    res, _ = triple("agent2_explain", a, b, c, default=GENERIC_EXPLAIN)
    return res


FAQ = [
    (("upload", "host", "deploy"), "📤 Tap Upload File and send your .py file. We scan it, fix simple mistakes, install libraries and start it."),
    (("premium", "price", "buy", "plan"), "💎 Tap Buy Premium to see plans. Payment is automatic via UPI."),
    (("token",), "🔑 Get your bot token from @BotFather. Never share it with anyone."),
    (("crash", "error", "stopped", "log"), "📜 Open My Bots → your bot → Logs to see why it stopped. Auto-restart is on by default."),
    (("refer", "free", "credit", "slot"), "🎁 Every friend who joins with your Refer link gives you +1 permanent slot."),
    (("docker", "container", "isolation"), "🖥 Docker is disabled in low-RAM mode. Hosted bots run as monitored subprocesses; avoid untrusted files and keep resource limits enabled."),
]




# ===================== [6] DOCKER MANAGER =====================
class DockerManager:
    """Use the preinstalled Docker SDK when available; never auto-install packages at runtime.
    If Docker is unavailable, the supervisor falls back to resource-monitored subprocesses."""

    def __init__(self):
        self.client = None
        self.ok = False
        self.reason = "disabled to save RAM; subprocess mode enabled"
        self._lock = threading.RLock()
        self._base_ready = False
        if DOCKER_ENABLED:
            self.connect()

    def _cname(self, bot_id, name):
        safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", name)[:30]
        return f"lxbot_{bot_id}_{safe}"[:60]

    def connect(self):
        global docker
        with self._lock:
            if docker is None:
                if not DOCKER_INSTALL_SDK_IF_MISSING:
                    self.ok = False
                    self.reason = "docker SDK missing"
                    return False
                try:
                    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "docker"],
                                   capture_output=True, timeout=240)
                    docker = importlib.import_module("docker")
                except Exception as e:
                    self.ok = False
                    self.reason = f"pip install docker failed: {e}"
                    return False
            try:
                self.client = docker.from_env()
                self.client.ping()
                self.ok = True
                self.reason = "connected"
                log.info("DockerManager connected: %s", self.client.version().get("Version", "?"))
                return True
            except Exception as e:
                self.ok = False
                self.reason = f"daemon unreachable: {e}"
                return False

    def _ensure_base(self):
        if self._base_ready:
            return True
        try:
            self.client.images.get(DOCKER_BASE_IMAGE)
            self._base_ready = True
            return True
        except Exception:
            pass
        try:
            log.info("Pulling base image %s ...", DOCKER_BASE_IMAGE)
            self.client.images.pull(DOCKER_BASE_IMAGE)
            self._base_ready = True
            return True
        except Exception as e:
            log.warning("Could not pull %s: %s", DOCKER_BASE_IMAGE, e)
            return False

    def _spec(self, uid, premium, god_mode):
        """Keep every container capped, including admin containers, to protect the small host."""
        if premium or god_mode:
            return {"mem": DOCKER_MEM_PREMIUM, "nano": int(DOCKER_CPUS_PREMIUM * 1e9)}
        return {"mem": DOCKER_MEM_FREE_FALLBACK, "nano": int(DOCKER_CPUS_FREE_FALLBACK * 1e9)}

    def run(self, bot_id, uid, name, bot_dir, god_mode=False, premium=False):
        if not self.ok and not self.connect():
            return None
        if not self._ensure_base():
            return None
        self.remove(bot_id, name)   # clean any stale container
        cname = self._cname(bot_id, name)
        spec = self._spec(uid, premium, god_mode)
        cmd = ("sh -lc '"
               "if [ ! -x /app/.venv_docker/bin/python ]; then python -m venv /app/.venv_docker; fi; "
               ". /app/.venv_docker/bin/activate; "
               "(pip install -q --disable-pip-version-check -r requirements.txt 2>/dev/null || true); "
               f"exec python -u {name}.py'")
        kwargs = dict(
            image=DOCKER_BASE_IMAGE,
            command=cmd,
            name=cname,
            volumes={bot_dir: {"bind": "/app", "mode": "rw"}},
            working_dir="/app",
            detach=True,
            environment={"PYTHONUNBUFFERED": "1"},
            restart_policy={"Name": "no"},
            network_disabled=False,
            # security hardening
            cap_drop=["ALL"] if not god_mode else None,
            security_opt=["no-new-privileges"] if not god_mode else None,
        )
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        if spec["mem"]:
            kwargs["mem_limit"] = spec["mem"]
        if spec["nano"]:
            kwargs["nano_cpus"] = spec["nano"]
        try:
            c = self.client.containers.run(**kwargs)
            log.info("Docker started %s (god=%s premium=%s)", cname, god_mode, premium)
            return c
        except Exception as e:
            log.exception("docker run failed for %s", cname)
            self.reason = f"run failed: {e}"
            return None

    def remove(self, bot_id, name):
        if not self.ok:
            return
        cname = self._cname(bot_id, name)
        try:
            c = self.client.containers.get(cname)
            try:
                c.stop(timeout=5)
            except Exception:
                pass
            try:
                c.remove(force=True)
            except Exception:
                pass
        except Exception:
            pass

    def stop_container(self, container):
        if container is None:
            return
        try:
            container.stop(timeout=5)
        except Exception:
            pass
        try:
            container.remove(force=True)
        except Exception:
            pass

    def logs(self, bot_id, name, tail=2000):
        if not self.ok:
            return ""
        try:
            c = self.client.containers.get(self._cname(bot_id, name))
            return c.logs(tail=tail).decode("utf-8", "replace")
        except Exception:
            return ""

    def stats(self, container):
        if container is None:
            return None
        try:
            s = container.stats(stream=False)
            mem = s.get("memory_stats", {}).get("usage", 0) / 2**20
            cpu_delta = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
            sys_delta = s["cpu_stats"]["system_cpu_usage"] - s["precpu_stats"]["system_cpu_usage"]
            ncpu = s["cpu_stats"].get("online_cpus") or len(s["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))
            cpu_pct = (100.0 * cpu_delta / sys_delta * ncpu) if sys_delta > 0 else 0.0
            return {"rss": mem, "cpu": cpu_pct}
        except Exception:
            return None

    def list_containers(self):
        if not self.ok:
            return []
        try:
            return self.client.containers.list(all=True, filters={"name": "lxbot_"})
        except Exception:
            return []

    def status_text(self):
        if not self.ok:
            return f"🐳 Docker: ❌ unavailable — {self.reason}"
        try:
            v = self.client.version().get("Version", "?")
            n = len(self.list_containers())
            return f"🐳 Docker: ✅ v{v} • {n} lxbot containers"
        except Exception as e:
            return f"🐳 Docker: ⚠️ degraded — {e}"


docker_mgr = DockerManager()


def use_docker_for(uid):
    """Premium users and admins get Docker isolation. Everyone else falls back to subprocess."""
    if not DOCKER_ENABLED or not docker_mgr.ok:
        return False
    return is_admin(uid) or is_premium(uid)


# ===================== [2] AGENT 3 - LOAD MANAGER =====================
def system_stats():
    def a():
        vm, du = psutil.virtual_memory(), psutil.disk_usage(BASE_DIR)
        return {"ram_pct": vm.percent, "ram_used": vm.used / 2**20, "ram_total": vm.total / 2**20,
                "cpu": psutil.cpu_percent(interval=None), "disk_pct": du.percent,
                "load": os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0), "cores": psutil.cpu_count() or 1}

    def b():
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0]) / 1024
        tot, av = mem["MemTotal"], mem.get("MemAvailable", mem.get("MemFree", 0))
        du = shutil.disk_usage(BASE_DIR)
        ld = os.getloadavg()
        cores = os.cpu_count() or 1
        return {"ram_pct": 100 * (tot - av) / tot, "ram_used": tot - av, "ram_total": tot,
                "cpu": min(100, 100 * ld[0] / cores), "disk_pct": 100 * du.used / du.total, "load": ld, "cores": cores}

    def c():
        return {"ram_pct": 0, "ram_used": 0, "ram_total": 0, "cpu": 0, "disk_pct": 0, "load": (0, 0, 0), "cores": 1}
    res, _ = triple("system_stats", a, b, c, default=c())
    return res


class LoadManager(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="LoadManager")
        self.rss, self.cpu, self.strikes, self.pcache = {}, {}, {}, {}
        self.last_kills = []
        if psutil:
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                pass

    def _p(self, pid):
        p = self.pcache.get(pid)
        if p is None:
            p = psutil.Process(pid)
            try:
                p.cpu_percent(None)
            except Exception:
                pass
            self.pcache[pid] = p
        return p

    def sample(self, pid):
        def a():
            root = self._p(pid)
            tree = [root] + root.children(recursive=True)
            rss = cpu = 0
            for x in tree:
                try:
                    px = self._p(x.pid)
                    rss += px.memory_info().rss
                    cpu += px.cpu_percent(None)
                except Exception:
                    continue
            return {"rss": rss / 2**20, "cpu": cpu, "status": root.status()}

        def b():
            with open(f"/proc/{pid}/statm") as f:
                pages = int(f.read().split()[1])
            return {"rss": pages * os.sysconf("SC_PAGE_SIZE") / 2**20, "cpu": 0, "status": "unknown"}
        res, _ = triple("sample_proc", a if psutil else None, b, None, default=None)
        return res

    def _kill(self, bot_id, ctl, reason, human):
        b = get_bot(bot_id)
        ctl["kill_reason"] = reason
        # Stop the supervisor from restarting an over-limit bot in a tight crash loop.
        ctl["stop"].set()
        kill_runtime(ctl)
        if b:
            run("UPDATE bots SET status='crashed', autorestart=0 WHERE id=?", (bot_id,))
        uid = b["user_id"] if b else None
        log_event("kill", uid, bot_id, f"{reason}: {human}")
        self.last_kills.append((time.time(), bot_id, reason))
        self.last_kills = self.last_kills[-20:]
        if b:
            safe_send(b["user_id"],
                      f"🛡 <b>Resource limit reached</b> for <b>{esc(b['name'])}</b>.\n{esc(human)}\n\n"
                      "Your bot has been paused to protect other users and the server. Reduce its memory/CPU use, "
                      "then restart it from My Bots. Premium has a higher RAM limit.")
            notify_admins(f"🛡 Load Manager killed bot <b>{esc(b['name'])}</b> "
                          f"(user <code>{b['user_id']}</code>): {esc(human)}")

    def check(self, force=False):
        actions = []
        with procs_lock:
            items = [(bid, ctl) for bid, ctl in procs.items()]
        sysd = system_stats()
        seen_pids, samples = set(), []
        for bid, ctl in items:
            if ctl.get("mode") == "docker":
                c = ctl.get("container")
                if c is None:
                    continue
                s = docker_mgr.stats(c)
                if not s:
                    continue
                self.rss[bid], self.cpu[bid] = s["rss"], s["cpu"]
                samples.append((bid, ctl, {"rss": s["rss"], "cpu": s["cpu"], "status": "running"}))
                continue
            proc = ctl.get("proc")
            if not proc or proc.poll() is not None:
                self.rss.pop(bid, None)
                self.strikes.pop(bid, None)
                continue
            seen_pids.add(proc.pid)
            s = self.sample(proc.pid)
            if not s:
                continue
            self.rss[bid], self.cpu[bid] = s["rss"], s["cpu"]
            samples.append((bid, ctl, s))
        for pid in list(self.pcache):
            if pid not in seen_pids:
                try:
                    if not self.pcache[pid].is_running():
                        self.pcache.pop(pid, None)
                except Exception:
                    self.pcache.pop(pid, None)
        killed = set()
        for bid, ctl, s in samples:
            b = get_bot(bid)
            if not b:
                continue
            god = is_admin(b["user_id"])
            if god:
                continue     # God Mode: never killed by the Load Manager
            limit = BOT_RAM_LIMIT_MB * (2 if is_premium(b["user_id"]) else 1)
            frozen_state = s["status"] in ("stopped", "zombie")
            self.strikes[bid] = self.strikes.get(bid, 0) + 1 if s["cpu"] >= FROZEN_CPU_PCT else 0
            if s["rss"] > limit:
                human = f"It used {s['rss']:.0f} MB of RAM (limit {limit} MB)."
                self._kill(bid, ctl, "memory", human)
            elif frozen_state or self.strikes[bid] >= FROZEN_STRIKES:
                human = "It appeared frozen (not responding / stuck at 100% CPU)."
                self._kill(bid, ctl, "frozen", human)
                self.strikes[bid] = 0
            else:
                continue
            killed.add(bid)
            actions.append(f"{b['name']}: {human}")
        if sysd["ram_pct"] >= SYSTEM_RAM_CRITICAL:
            rest = [(bid, ctl, s) for bid, ctl, s in samples
                    if bid not in killed and not is_admin((get_bot(bid) or {"user_id": 0})["user_id"])]
            if rest:
                bid, ctl, s = max(rest, key=lambda t: t[2]["rss"])
                b = get_bot(bid)
                human = f"The server is almost out of RAM ({sysd['ram_pct']:.0f}%), and this bot was the heaviest ({s['rss']:.0f} MB)."
                self._kill(bid, ctl, "pressure", human)
                actions.append(f"{b['name'] if b else bid}: {human}")
        return actions

    def run(self):
        while True:
            time.sleep(MONITOR_INTERVAL)
            try:
                self.check()
            except Exception:
                log.exception("LoadManager pass failed (continuing)")


load_manager = LoadManager()


# ------------------------------ FamGateway ------------------------------
def fg_create_order(amount, customer_name=None):
    for attempt in (1, 2):
        try:
            r = requests.post(f"{FAM_GATEWAY_BASE}/api/create-order",
                              headers={"X-Api-Key": FAM_GATEWAY_API_KEY, "Content-Type": "application/json"},
                              json={"amount": float(amount), "customer_name": (customer_name or "")[:60]},
                              timeout=NET_TIMEOUT)
            data = r.json()
            if data.get("status") == "success":
                return data["data"]
            log.warning("FamGateway create_order failed: %s", data)
        except Exception:
            log.exception("FamGateway create_order error (attempt %s)", attempt)
        time.sleep(1)
    return None


def fg_verify_order(order_id):
    try:
        r = requests.get(f"{FAM_GATEWAY_BASE}/api/verify-order.php",
                         params={"api_key": FAM_GATEWAY_API_KEY, "order_id": order_id}, timeout=NET_TIMEOUT)
        return r.json()
    except Exception:
        log.exception("FamGateway verify_order error")
        return {"status": "error"}


def activate_premium(uid, days):
    u = get_user(uid)
    base = utcnow()
    if u and u["premium_until"]:
        try:
            base = max(base, datetime.fromisoformat(u["premium_until"]))
        except ValueError:
            pass
    until = base + timedelta(days=days)
    run("UPDATE users SET premium_until=? WHERE user_id=?", (until.isoformat(), uid))
    return until


def poll_payment(uid, chat, mid, order_id, plan):
    deadline = time.time() + PAYMENT_POLL_MAX_MIN * 60
    expired_txt = "⌛ <b>Payment session expired.</b>\n\nTap 💎 Buy Premium to try again."
    while time.time() < deadline:
        time.sleep(PAYMENT_POLL_SECONDS)
        cur = rows("SELECT status FROM orders WHERE order_id=?", (order_id,))
        if not cur or cur[0]["status"] != "pending":
            return
        res = fg_verify_order(order_id)
        st = res.get("status")
        if st == "success":
            data = res.get("data", {})
            run("UPDATE orders SET status='paid', paid_at=?, utr=? WHERE order_id=?",
                (utcnow().isoformat(), data.get("utr", ""), order_id))
            until = activate_premium(uid, plan["days"])
            log_event("payment", uid, None, f"{plan['label']} utr={data.get('utr', '')}")
            cap = f"✅ <b>Payment Verified!</b>\n\n{DIV}\n🧾 UTR: <code>{esc(data.get('utr',''))}</code>\n" \
                  f"💎 Plan: <b>{esc(plan['label'])}</b>\n{DIV}\n\n🚀 Premium active until <b>{until:%Y-%m-%d}</b>!\n" \
                  "🐳 Your next bots will run in isolated Docker containers."
            try:
                bot.edit_message_caption(caption=cap, chat_id=chat, message_id=mid, reply_markup=None)
            except Exception:
                safe_send(chat, cap)
            send_sticker_safe(chat, STICKER_PREMIUM)
            notify_admins(f"💰 <b>Payment received</b>\nUser: <code>{uid}</code>\n"
                          f"Plan: {esc(plan['label'])}\nUTR: {esc(data.get('utr',''))}")
            return
        if st == "expired":
            break
    run("UPDATE orders SET status='expired' WHERE order_id=? AND status='pending'", (order_id,))
    try:
        bot.edit_message_caption(caption=expired_txt, chat_id=chat, message_id=mid, reply_markup=None)
    except Exception:
        pass


def send_payment_card(chat, order, caption, markup):
    def a():
        return bot.send_photo(chat, order["qr_url"], caption=caption, reply_markup=markup)

    def b():
        img = requests.get(order["qr_url"], timeout=NET_TIMEOUT).content
        return bot.send_photo(chat, img, caption=caption, reply_markup=markup)

    def c():
        return bot.send_message(chat, caption, reply_markup=markup)
    res, _ = triple("payment_card", a, b, c)
    return res


# =================== [3] HOSTED BOT SUPERVISOR (Docker + subprocess) ===================
procs = {}
procs_lock = threading.RLock()


def get_bot(bot_id, uid=None):
    r = rows("SELECT * FROM bots WHERE id=?", (bot_id,))
    if not r or (uid is not None and r[0]["user_id"] != uid):
        return None
    return r[0]


def kill_proc(proc):
    if not proc or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        pass
    try:
        proc.wait(5)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(5)
        except Exception:
            pass


def kill_runtime(ctl):
    """Stop whichever runtime is active: docker container or subprocess. Never raises."""
    c = ctl.get("container")
    if c is not None:
        try:
            docker_mgr.stop_container(c)
        except Exception:
            pass
        ctl["container"] = None
    p = ctl.get("proc")
    if p is not None:
        try:
            kill_proc(p)
        except Exception:
            pass
        ctl["proc"] = None


def tail(path, n=3500):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def launch(d, entry, lp):
    tried, last = set(), None
    for py in python_candidates(d):
        for pre in (limiter(), None):
            key = (py, pre is not None)
            if key in tried:
                continue
            tried.add(key)
            try:
                with open(lp, "ab") as logf:
                    kind = "venv" if py == venv_python(d) else "system"
                    logf.write(f"\n--- start {utcnow().isoformat(timespec='seconds')} UTC ({kind} python) ---\n".encode())
                    logf.flush()
                    return subprocess.Popen(
                        [py, "-u", entry], cwd=d, env=bot_env(d), stdout=logf, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, close_fds=True, start_new_session=True, preexec_fn=pre)
            except Exception as e:
                last = e
                log.warning("launch via %s (limits=%s) failed: %s", py, pre is not None, e)
    raise RuntimeError(f"all launch methods failed: {last}")


def notify_crash(uid, name, lp, why):
    t = (tail(lp, 500).strip() or "(no output)")[-400:]
    friendly = agent2_explain(tail(lp, 1500))
    safe_send(uid, f"⚠️ <b>{esc(name)}</b> {esc(why)}.\n\n💡 {friendly}\n\n📄 Last output:\n<pre>{esc(t)}</pre>")


def notify_autofix_start(uid, name, pkg):
    try:
        msg = bot.send_message(uid, f"⚠️ <b>{esc(name)}</b> crashed — 🔎 Analyzing crash log...")
        time.sleep(1.3)
        bot.edit_message_text(f"🤖 <b>Auto-Fix Assistant</b> diagnosing <b>{esc(name)}</b>...\n"
                              f"📦 Missing module detected: <code>{esc(pkg)}</code>", uid, msg.message_id)
        time.sleep(1.3)
        bot.edit_message_text(f"🛠 <b>Auto-Fix Assistant</b> installing <code>{esc(pkg)}</code> "
                              f"for <b>{esc(name)}</b>...", uid, msg.message_id)
        return msg.message_id
    except Exception:
        log.warning("notify_autofix_start failed", exc_info=True)
        return None


def notify_autofix_result(uid, mid, name, pkg, ok):
    if mid is None:
        return
    try:
        if ok:
            bot.edit_message_text(f"✅ <b>Auto-Fix Assistant</b> fixed <b>{esc(name)}</b>! "
                                  f"Installed <code>{esc(pkg)}</code> and restarted the instance.", uid, mid)
        else:
            bot.edit_message_text(f"⚠️ Auto-Fix couldn't install <code>{esc(pkg)}</code> for "
                                  f"<b>{esc(name)}</b> automatically. Check 📜 Logs in My Bots for details.", uid, mid)
    except Exception:
        log.warning("notify_autofix_result failed", exc_info=True)


def runtime_syntax_fix(uid, bot_id, name, d, lp, seen):
    if not re.search(r"(SyntaxError|IndentationError|TabError)", tail(lp, 3000)):
        return False
    path = os.path.join(d, name + ".py")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()
    h = hashlib.sha1(src.encode("utf-8", "replace")).hexdigest()
    if h in seen:
        return False
    seen.add(h)
    new, notes = agent1_fix(src)
    if not notes:
        return False
    ok, _, _ = agent4_scan(new)
    if not ok:
        return False
    write_user_file(d, name + ".py", new.encode("utf-8"))
    log_event("codefix", uid, bot_id, "runtime: " + ", ".join(notes))
    safe_send(uid, f"🧩 <b>Code Fixer</b> repaired <b>{esc(name)}</b> ({esc(', '.join(notes))}) and re-deployed it.")
    return True


def _run_docker_supervisor(bot_id, ctl, b, uid, name, d, god, premium):
    """One iteration in Docker mode. Returns when the container exits."""
    c = docker_mgr.run(bot_id, uid, name, d, god_mode=god, premium=premium)
    if c is None:
        ctl["mode"] = "subprocess"
        return False
    ctl["container"] = c
    ctl["started"] = time.time()
    run("UPDATE bots SET runtime='docker' WHERE id=?", (bot_id,))
    if ctl["stop"].is_set():
        docker_mgr.stop_container(c)
        ctl["container"] = None
        return True
    # wait for exit (blocking). Docker SDK wait has no default timeout — safe here.
    try:
        c.wait()
    except Exception:
        pass
    ctl["started"] = None
    # capture last 4 KB of logs into the log file so downstream crash analysis sees it
    try:
        logs = docker_mgr.logs(bot_id, name, tail=200).encode("utf-8", "replace")
        with open(log_path(bot_id), "ab") as lf:
            lf.write(b"\n--- docker tail ---\n" + logs + b"\n")
    except Exception:
        pass
    docker_mgr.stop_container(c)
    ctl["container"] = None
    return True


def supervise(bot_id, ctl):
    """One thread per hosted bot. Runs inside its own try/except world."""
    restarts, fixed_pkgs, fixed_src = [], set(), set()
    final = "stopped"
    try:
        while not ctl["stop"].is_set():
            b = get_bot(bot_id)
            u = get_user(b["user_id"]) if b else None
            if not b or not u or u["banned"]:
                break
            uid, name = b["user_id"], b["name"]
            d = bot_dir(uid, name)
            entry = name + ".py"
            lp = log_path(bot_id)
            god = is_admin(uid)
            premium = is_premium(uid)
            ctl["mode"] = "docker" if use_docker_for(uid) else "subprocess"
            try:
                if os.path.exists(lp) and os.path.getsize(lp) > 5 * 1024 * 1024:
                    open(lp, "wb").close()
                os.makedirs(os.path.join(d, ".tmp"), exist_ok=True)
                if ctl["mode"] == "docker":
                    ok = _run_docker_supervisor(bot_id, ctl, b, uid, name, d, god, premium)
                    if not ok:
                        # Docker fell over -> fall back to subprocess this round
                        log.warning("Docker run failed for bot %s, falling back to subprocess", bot_id)
                        run("UPDATE bots SET runtime='subprocess' WHERE id=?", (bot_id,))
                else:
                    if not os.path.exists(venv_python(d)):
                        try:
                            ensure_venv(d)
                        except Exception:
                            log.exception("venv step failed; using system python + libs folder")
                    proc = launch(d, entry, lp)
                    ctl["proc"] = proc
                    ctl["started"] = time.time()
                    run("UPDATE bots SET runtime='subprocess' WHERE id=?", (bot_id,))
                    if ctl["stop"].is_set():
                        kill_proc(proc)
                        ctl["proc"] = None
                        break
                    proc.wait()
                    ctl["started"] = None
                    ctl["proc"] = None
            except Exception as e:
                log.exception("supervisor error for bot %s", bot_id)
                try:
                    with open(lp, "ab") as lf:
                        lf.write(f"\n[host] could not start the bot: {e}\n".encode())
                except OSError:
                    pass
            if ctl["stop"].is_set():
                break
            killed_by_manager = ctl.pop("kill_reason", None)
            if not killed_by_manager:
                mod = extract_missing_module(tail(lp, 3000))
                if mod:
                    pkg = PIP_MAP.get(mod, mod)
                    if pkg not in fixed_pkgs and PKG_RE.match(pkg):
                        fixed_pkgs.add(pkg)
                        fix_mid = notify_autofix_start(uid, name, pkg)
                        ok_pkgs = []
                        try:
                            ok_pkgs, _ = pip_install(d, [pkg])
                        except Exception:
                            log.exception("auto-install failed")
                        log_event("autofix", uid, bot_id, f"{pkg} {'ok' if ok_pkgs else 'failed'}")
                        notify_autofix_result(uid, fix_mid, name, pkg, bool(ok_pkgs))
                        continue
                try:
                    if runtime_syntax_fix(uid, bot_id, name, d, lp, fixed_src):
                        continue
                except Exception:
                    log.exception("runtime fix failed")
            b = get_bot(bot_id)
            if not b or not AUTO_RESTART or not b["autorestart"]:
                final = "crashed"
                log_event("crash", uid, bot_id, "stopped, auto-restart off")
                notify_crash(uid, name, lp, "stopped (auto-restart is off)")
                break
            nowt = time.time()
            restarts = [t for t in restarts if nowt - t < RESTART_WINDOW_SEC] + [nowt]
            if len(restarts) > MAX_RESTARTS:
                final = "crashed"
                log_event("crash", uid, bot_id, f"crash loop ({MAX_RESTARTS}x)")
                notify_crash(uid, name, lp, f"crashed {MAX_RESTARTS} times in a row and was stopped")
                break
            ctl["stop"].wait(min(5 * len(restarts), 60))
    except Exception:
        log.exception("supervisor fatal for bot %s", bot_id)
        final = "crashed"
    finally:
        kill_runtime(ctl)
        if not ctl["stop"].is_set():
            run("UPDATE bots SET status=? WHERE id=?", (final, bot_id))
        # Always release the in-memory supervisor entry, including resource-limit pauses.
        with procs_lock:
            if procs.get(bot_id) is ctl:
                procs.pop(bot_id, None)


def start_bot(bot_id):
    def spawn():
        with procs_lock:
            ctl = procs.get(bot_id)
            if ctl and ctl["thread"].is_alive():
                return False
            ctl = {"stop": threading.Event(), "proc": None, "container": None,
                   "started": None, "mode": "subprocess"}
            ctl["thread"] = threading.Thread(target=supervise, args=(bot_id, ctl), daemon=True,
                                             name=f"bot-{bot_id}")
            procs[bot_id] = ctl
            run("UPDATE bots SET status='running' WHERE id=?", (bot_id,))
            ctl["thread"].start()
        return True

    def retry():
        time.sleep(2)
        return spawn()

    def give_up():
        run("UPDATE bots SET status='crashed' WHERE id=?", (bot_id,))
        raise RuntimeError("could not start supervisor")
    res, _ = triple("start_bot", spawn, retry, give_up, default=False)
    return res


def stop_bot(bot_id, status="stopped"):
    run("UPDATE bots SET status=? WHERE id=?", (status, bot_id))
    with procs_lock:
        ctl = procs.pop(bot_id, None)
    if ctl:
        ctl["stop"].set()
        kill_runtime(ctl)
        ctl["thread"].join(timeout=15)
    run("UPDATE bots SET status=? WHERE id=?", (status, bot_id))


# ===================== STORAGE GARBAGE COLLECTION (RAM + DISK) =====================
def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _inside_hosted(path):
    """Safety rail: we only ever delete things that live INSIDE hosted_bots/."""
    try:
        root = os.path.realpath(HOSTED_DIR)
        p = os.path.realpath(path)
        return p != root and p.startswith(root + os.sep)
    except Exception:
        return False


def _force_writable(func, p, _exc):
    try:
        os.chmod(p, 0o700)
        func(p)
    except Exception:
        pass


def safe_rmtree(path):
    """Delete a directory tree (only inside hosted_bots/). Plan A: normal rmtree. Plan B: chmod + retry.
    Plan C: remove root-owned files that Docker containers created, using a throw-away container.
    Returns the number of bytes freed (0 if nothing was removed)."""
    if not _inside_hosted(path) or not os.path.exists(path):
        return 0
    freed = dir_size(path)
    shutil.rmtree(path, onerror=_force_writable)
    if os.path.exists(path) and docker_mgr.ok:
        try:
            docker_mgr.client.containers.run(
                DOCKER_BASE_IMAGE, ["sh", "-c", "rm -rf /w/* /w/.[!.]* /w/..?* 2>/dev/null; true"],
                volumes={os.path.realpath(path): {"bind": "/w", "mode": "rw"}},
                remove=True, network_disabled=True)
        except Exception as e:
            log.warning("docker cleanup of %s failed: %s", path, e)
        shutil.rmtree(path, onerror=_force_writable)
    if os.path.exists(path):
        log.warning("could not fully delete %s", path)
        return 0
    return freed


def wipe_bot_files(uid, name, bot_id=None):
    """Completely remove everything the platform keeps for ONE hosted bot: its container, its folder
    (source, venv, libs, temp files) and its log file. Returns bytes freed."""
    if bot_id is not None:
        try:
            docker_mgr.remove(bot_id, name)
        except Exception:
            pass
        try:
            os.remove(log_path(bot_id))
        except OSError:
            pass
    freed = safe_rmtree(os.path.join(HOSTED_DIR, str(uid), name))
    try:
        os.rmdir(os.path.join(HOSTED_DIR, str(uid)))     # succeeds only when the user folder is empty
    except OSError:
        pass
    return freed


# ---- bot-token fingerprints: used to detect "same bot, new file" (raw tokens are never stored) ----
def token_hashes(src):
    out = []
    for t in BOT_TOKEN_RE.findall(src or ""):
        h = hashlib.sha256(t.encode("utf-8", "replace")).hexdigest()
        if h not in out:
            out.append(h)
    return out


def pack_hashes(hs):
    return (" " + " ".join(hs) + " ") if hs else "-"


def backfill_token_hashes():
    """Give already-hosted bots (from older versions) a token fingerprint."""
    for b in rows("SELECT id,user_id,name FROM bots WHERE token_hash IS NULL"):
        packed = "-"
        try:
            p = os.path.join(HOSTED_DIR, str(b["user_id"]), b["name"], b["name"] + ".py")
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                packed = pack_hashes(token_hashes(f.read()))
        except OSError:
            pass
        run("UPDATE bots SET token_hash=? WHERE id=?", (packed, b["id"]))


def sweep_orphans(min_age=None):
    """Delete everything on disk / in Docker that no database row owns: orphan bot folders, stray temp
    files, orphan logs and orphan containers. Returns (bytes_freed, [what was removed])."""
    min_age = SWEEP_MIN_AGE_SEC if min_age is None else min_age
    now = time.time()
    freed, removed = 0, []
    valid = {(str(r["user_id"]), r["name"]) for r in rows("SELECT user_id,name FROM bots")}
    ids = {r["id"] for r in rows("SELECT id FROM bots")}

    def old(p):
        try:
            return now - os.path.getmtime(p) >= min_age
        except OSError:
            return False

    try:
        user_dirs = os.listdir(HOSTED_DIR)
    except OSError:
        user_dirs = []
    for u in user_dirs:
        up = os.path.join(HOSTED_DIR, u)
        if not os.path.isdir(up):
            continue
        try:
            entries = os.listdir(up)
        except OSError:
            continue
        for e in entries:
            ep = os.path.join(up, e)
            if (u, e) in valid:
                try:                                         # stale upload temp files inside a live bot
                    for f in os.listdir(ep):
                        fp = os.path.join(ep, f)
                        if f.startswith(".up_") and old(fp):
                            freed += os.path.getsize(fp)
                            os.remove(fp)
                except OSError:
                    pass
                continue
            if not old(ep):
                continue
            if os.path.isdir(ep):
                freed += safe_rmtree(ep)
            else:
                try:
                    freed += os.path.getsize(ep)
                    os.remove(ep)
                except OSError:
                    continue
            removed.append(f"{u}/{e}")
        try:
            os.rmdir(up)
        except OSError:
            pass
    try:
        for f in os.listdir(LOGS_DIR):
            m = re.fullmatch(r"(\d+)\.log", f)
            fp = os.path.join(LOGS_DIR, f)
            if m and int(m.group(1)) not in ids and old(fp):
                freed += os.path.getsize(fp)
                os.remove(fp)
                removed.append(f"log {f}")
    except OSError:
        pass
    if docker_mgr.ok:
        for c in docker_mgr.list_containers():
            try:
                m = re.match(r"lxbot_(\d+)_", c.name)
                if m and int(m.group(1)) not in ids:
                    docker_mgr.stop_container(c)
                    removed.append(f"container {c.name}")
            except Exception:
                pass
    gc.collect()
    return freed, removed


def stop_all_runtimes_keep_db():
    """Stop every hosted process/container WITHOUT touching the 'running' status in the DB, so the next
    boot (recover_bots) brings them all back. Used right before the OTA self-restart."""
    with procs_lock:
        items = list(procs.values())
    for c in items:
        c["stop"].set()
    for c in items:
        kill_runtime(c)


def delete_bot(b):
    """Stop + remove a bot completely. NOTE: this never gives a free credit back (lifetime_free_used is
    left untouched on purpose)."""
    stop_bot(b["id"])
    wipe_bot_files(b["user_id"], b["name"], b["id"])
    run("DELETE FROM bots WHERE id=?", (b["id"],))
    gc.collect()


def ban_user(uid):
    run("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
    for b in rows("SELECT id FROM bots WHERE user_id=?", (uid,)):
        stop_bot(b["id"])


def recover_bots():
    for b in rows("SELECT b.id FROM bots b JOIN users u ON u.user_id=b.user_id "
                  "WHERE b.status='running' AND u.banned=0"):
        start_bot(b["id"])
        time.sleep(0.3)


def shutdown(*_):
    log.info("Shutting down, stopping hosted bots...")
    with procs_lock:
        items = list(procs.values())
    for c in items:
        c["stop"].set()
    for c in items:
        kill_runtime(c)
    os._exit(0)


def keep_alive(target, name):
    """Run `target` forever in a daemon thread; if it ever dies, log and restart it. Used for every worker."""
    def loop():
        backoff = 2
        while True:
            try:
                target()
                backoff = 2
            except BaseException:
                log.exception("thread '%s' crashed, restarting in %ss", name, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
    t = threading.Thread(target=loop, daemon=True, name=name)
    t.start()
    return t


def guardian_pass():
    """Revive supervisors that died while the DB still says 'running'. Also pokes Docker to reconnect."""
    if DOCKER_ENABLED and not docker_mgr.ok:
        try:
            docker_mgr.connect()
        except Exception:
            log.debug("docker reconnect attempt failed")
    for b in rows("SELECT b.id FROM bots b JOIN users u ON u.user_id=b.user_id "
                  "WHERE b.status='running' AND u.banned=0"):
        with procs_lock:
            ctl = procs.get(b["id"])
            alive = bool(ctl and ctl["thread"].is_alive())
        if not alive:
            log.warning("Guardian: reviving supervisor for bot %s", b["id"])
            log_event("guardian", None, b["id"], "supervisor revived")
            start_bot(b["id"])


# ======================== [2] AGENT 5 - ADMIN ADVISOR ========================
def advisor_snapshot():
    since = (utcnow() - timedelta(hours=24)).isoformat()
    ev = {r["kind"]: r["c"] for r in rows("SELECT kind, COUNT(*) c FROM events WHERE ts>? GROUP BY kind", (since,))}
    snap = {
        "users": rows("SELECT COUNT(*) c FROM users")[0]["c"],
        "premium": rows("SELECT COUNT(*) c FROM users WHERE premium_until > ?", (utcnow().isoformat(),))[0]["c"],
        "banned": rows("SELECT COUNT(*) c FROM users WHERE banned=1")[0]["c"],
        "bots": rows("SELECT COUNT(*) c FROM bots")[0]["c"],
        "running": sum(1 for x in list(procs.values()) if x["thread"].is_alive()),
        "crashed": rows("SELECT COUNT(*) c FROM bots WHERE status='crashed'")[0]["c"],
        "events24h": ev, "queue": upload_q.qsize(), "sys": system_stats(),
        "top_ram": sorted(load_manager.rss.items(), key=lambda kv: -kv[1])[:3],
        "docker": docker_mgr.reason if not docker_mgr.ok else "connected",
    }
    return snap


def agent5_summary(use_ai=True):
    snap = advisor_snapshot()
    s, ev = snap["sys"], snap["events24h"]

    def a():
        if not use_ai:
            raise RuntimeError("AI skipped")
        facts = json.dumps({k: v for k, v in snap.items() if k != "top_ram"}, default=str)
        txt = ai_chat("You advise the owner of a Telegram bot-hosting service. From these 24h stats write a short "
                      "briefing (max 110 words): status, risks, and 2 recommendations.\n" + facts,
                      system="You are a concise DevOps advisor. Plain text, no markdown.")
        return "🧠 " + esc(txt[:1200])

    def b():
        tips = []
        if s["ram_pct"] >= 85:
            tips.append("RAM is high: consider lowering BOT_RAM_LIMIT_MB or limiting free uploads.")
        if snap["crashed"] >= 3:
            tips.append(f"{snap['crashed']} bots are in a crashed state: review their logs.")
        if ev.get("reject", 0) >= 5:
            tips.append(f"{ev['reject']} uploads were rejected by the scanner today: possible abuse, check Activity Log.")
        if ev.get("kill", 0) >= 3:
            tips.append(f"Load Manager stopped {ev['kill']} bots today: heavy users may need Premium or limits.")
        if snap["queue"] > 10:
            tips.append("The upload queue is long: raise UPLOAD_WORKERS if CPU allows.")
        if not docker_mgr.ok:
            tips.append(f"Docker is not available ({snap['docker']}); premium isolation is degraded.")
        if not tips:
            tips.append("Everything looks healthy. No action needed.")
        health = "🟢 Healthy" if s["ram_pct"] < 75 and snap["crashed"] < 3 else "🟡 Needs attention"
        return (f"🧠 <b>Advisor</b> — {health}\n"
                f"Uploads 24h: {ev.get('upload', 0)} • Rejected: {ev.get('reject', 0)} • Auto-fixes: "
                f"{ev.get('autofix', 0) + ev.get('codefix', 0)} • Kills: {ev.get('kill', 0)} • Crashes: {ev.get('crash', 0)}\n\n"
                + "\n".join("• " + esc(t) for t in tips))

    def c():
        return f"🧠 Users {snap['users']}, bots {snap['bots']} ({snap['running']} running), RAM {s['ram_pct']:.0f}%."
    res, _ = triple("agent5_summary", a, b, c, default=c())
    return res


def maintenance_loop():
    last_report, last_prune, last_sweep = time.time(), time.time(), 0.0
    while True:
        time.sleep(30)
        try:
            guardian_pass()
        except Exception:
            log.exception("guardian pass failed")
        try:
            if time.time() - last_sweep >= SWEEP_INTERVAL_SEC:
                last_sweep = time.time()
                freed, removed = sweep_orphans()
                if removed:
                    log.info("Sweeper removed %d orphan item(s), freed %.1f MB", len(removed), freed / 2**20)
                    log_event("sweep", None, None, f"{len(removed)} items, {freed / 2**20:.1f} MB")
                gc.collect()
        except Exception:
            log.exception("sweep failed")
        try:
            if DAILY_REPORT_HOURS and time.time() - last_report >= DAILY_REPORT_HOURS * 3600:
                last_report = time.time()
                notify_admins("📰 <b>Daily report</b>\n" + agent5_summary())
            if time.time() - last_prune >= 7 * 86400:
                last_prune = time.time()
                run("DELETE FROM events WHERE ts < ?", ((utcnow() - timedelta(days=30)).isoformat(),))
        except Exception:
            log.exception("maintenance failed")


# ---------------------------- Telegram: user side ----------------------
user_locks = {}


def ulock(uid):
    return user_locks.setdefault(uid, threading.Lock())


def send_menu(chat_id, first_name=""):
    bot.send_message(chat_id,
                     f"🚀 <b>LX HOSTING PRO</b>\n{DIV}\n\n"
                     f"Welcome{', ' + esc(first_name) if first_name else ''}! Host your Python Telegram "
                     "bots 24/7 — upload a <code>.py</code> file and our 5 smart agents handle security, "
                     "fixes, dependencies, crash recovery and uptime for you.\n\nChoose an option below 👇",
                     reply_markup=main_menu(chat_id))


def send_welcome(chat_id, text, markup=None):
    raw = get_setting("welcome_media")
    media = None
    try:
        media = json.loads(raw) if raw else None
    except ValueError:
        pass

    def send_media(caption=None, mk=None):
        if media["type"] == "video":
            return bot.send_video(chat_id, media["file_id"], caption=caption, reply_markup=mk, supports_streaming=True)
        return bot.send_animation(chat_id, media["file_id"], caption=caption, reply_markup=mk)

    def a():
        if not media:
            raise LookupError("no media set")
        return send_media(text, markup)

    def b():
        if not media:
            raise LookupError("no media set")
        send_media()
        return bot.send_message(chat_id, text, reply_markup=markup)

    def c():
        return bot.send_message(chat_id, text, reply_markup=markup)
    triple("send_welcome", a, b, c)


@bot.message_handler(commands=["start"])
@safe
def cmd_start(m):
    ref = None
    p = (m.text or "").split(maxsplit=1)
    if len(p) > 1 and p[1].startswith("ref_") and p[1][4:].isdigit():
        ref = int(p[1][4:])
    register(m.from_user, ref)
    if is_admin(m.from_user.id):
        run("UPDATE users SET confirmed=1, banned=0 WHERE user_id=?", (m.from_user.id,))
    u = get_user(m.from_user.id)
    if u["banned"]:
        return bot.send_message(m.chat.id, blocked(m.from_user.id))
    if u["confirmed"]:
        send_sticker_safe(m.chat.id, STICKER_START)
        return send_menu(m.chat.id, m.from_user.first_name)
    send_welcome(m.chat.id,
                 f"👋 <b>Welcome to LX Hosting PRO!</b>\n{DIV}\n\n"
                 "Join both channels below, then tap <b>I've Joined Both</b> to unlock the bot.",
                 join_markup())


@bot.message_handler(commands=["mybots"])
@safe
def cmd_mybots(m):
    why = blocked(m.from_user.id)
    if why:
        return bot.send_message(m.chat.id, why)
    show_bots(m.chat.id, m.from_user.id)


def show_bots(chat_id, uid, edit_mid=None):
    bl = rows("SELECT * FROM bots WHERE user_id=? ORDER BY id", (uid,))
    icon = {"running": "🟢", "stopped": "🔴", "crashed": "⚠️"}
    rws = [[btn(f"{icon.get(b['status'], '🔴')} {b['name']}", "primary", cb=f"mb:view:{b['id']}")] for b in bl]
    if is_admin(uid):
        quota = f"👑 Admin — unlimited slots"
    elif is_premium(uid):
        quota = f"{bot_count(uid)}/{premium_cap(uid)} active slots (💎 Premium)"
    else:
        quota = f"{free_remaining(uid)} lifetime free credit(s) left"
    text = (f"📂 <b>My Bots</b>\n{DIV}\n{quota}" if bl
            else f"📂 You have no bots yet. Send a <code>.py</code> file to host one.\n{DIV}\n{quota}")
    if edit_mid:
        bot.edit_message_text(text, chat_id, edit_mid, reply_markup=inline(rws) if rws else None)
    else:
        bot.send_message(chat_id, text, reply_markup=inline(rws) if rws else None)


def bot_uptime(bot_id):
    ctl = procs.get(bot_id)
    return fmt_dur(time.time() - ctl["started"]) if ctl and ctl.get("started") else "—"


def panel(b):
    st = {"running": "🟢 Running", "stopped": "🔴 Stopped", "crashed": "⚠️ Crashed (gave up restarting)"}
    ram = load_manager.rss.get(b["id"])
    rt = b["runtime"] or "subprocess"
    rt_icon = "🐳 Docker" if rt == "docker" else "🖥 Subprocess"
    if is_admin(b["user_id"]):
        rt_icon += " 👑 God Mode"
    public_id = b["public_id"] if "public_id" in b.keys() and b["public_id"] else ensure_public_bot_id(b["id"])
    status_url = f"{PUBLIC_BASE_URL}/status/{public_id}" if PUBLIC_BASE_URL else f"/status/{public_id}"
    text = (f"🤖 <b>{esc(b['name'])}</b>\n{DIV}\nStatus: {st.get(b['status'], b['status'])}\n"
            f"Runtime: {rt_icon}\n"
            f"⏱ Uptime: {bot_uptime(b['id'])}" + (f" • 🧠 {ram:.0f} MB" if ram and b["status"] == "running" else "") +
            f"\n🆔 Bot ID: <code>{esc(public_id)}</code>\n🌐 Status: <code>{esc(status_url)}</code>\n"
            f"\nAuto-restart: {'ON ✅' if b['autorestart'] else 'OFF ❌'}\nCreated: {esc(b['created'][:16])} UTC")
    i = b["id"]
    main_btn = [btn("⏹ Stop Bot", "danger", cb=f"mb:stop:{i}")] if b["status"] == "running" \
        else [btn("▶️ Start Bot", "success", cb=f"mb:start:{i}")]
    return text, inline([
        main_btn,
        [btn("🔄 Refresh", "primary", cb=f"mb:refresh:{i}"), btn("🔄 Restart", "primary", cb=f"mb:restart:{i}"), btn("📜 Logs", "primary", cb=f"mb:logs:{i}")],
        [btn(f"♻️ Auto-restart: {'ON' if b['autorestart'] else 'OFF'}", cb=f"mb:ar:{i}")],
        [btn("🗑 Delete", "danger", cb=f"mb:delq:{i}"), btn("⬅️ Back", cb="mb:list:0")],
    ])


def status_view(uid):
    bl = rows("SELECT * FROM bots WHERE user_id=? ORDER BY id", (uid,))
    icon = {"running": "🟢", "stopped": "🔴", "crashed": "⚠️"}
    if not bl:
        txt = f"📊 <b>My Bots Status</b>\n{DIV}\nNo bots yet. Tap 📤 Upload File to host your first one!"
    else:
        lines = []
        for b in bl:
            ram = load_manager.rss.get(b["id"])
            rt = "🐳" if b["runtime"] == "docker" else "🖥"
            lines.append(f"{icon.get(b['status'], '🔴')} {rt} <b>{esc(b['name'])}</b> — ⏱ {bot_uptime(b['id'])}"
                         + (f" • 🧠 {ram:.0f} MB" if ram and b["status"] == "running" else ""))
        running = sum(1 for b in bl if b["status"] == "running")
        txt = (f"📊 <b>My Bots Status</b>\n{DIV}\n" + "\n".join(lines) +
               f"\n{DIV}\n🟢 {running} running • 🔴 {len(bl) - running} not running")
    return txt, inline([[btn("🔄 Refresh", "primary", cb="st:refresh"), btn("📂 Manage", "success", cb="mb:list:0")]])


@bot.message_handler(func=lambda m: m.text in MYBOTS_TXT)
@safe
def h_mybots(m):
    why = blocked(m.from_user.id)
    if why:
        return bot.send_message(m.chat.id, why)
    show_bots(m.chat.id, m.from_user.id)


@bot.message_handler(func=lambda m: m.text == BTN_STATUS)
@safe
def h_status(m):
    why = blocked(m.from_user.id)
    if why:
        return bot.send_message(m.chat.id, why)
    t, mk = status_view(m.from_user.id)
    bot.send_message(m.chat.id, t, reply_markup=mk)


@bot.message_handler(func=lambda m: m.text == BTN_UPLOAD)
@safe
def h_upload(m):
    why = blocked(m.from_user.id)
    if why:
        return bot.send_message(m.chat.id, why)
    uid = m.from_user.id
    if is_admin(uid):
        quota = "👑 Admin — priority access (resource caps still protect the server)"
    elif is_premium(uid):
        quota = f"{bot_count(uid)}/{premium_cap(uid)} active slots (💎 Premium • 🐳 Docker)"
    else:
        quota = f"{free_remaining(uid)} lifetime free credit(s) left"
    bot.send_message(m.chat.id,
                     f"📤 <b>Upload your bot</b>\n{DIV}\n\nSend your <code>.py</code> file as a document "
                     "in this chat.\n"
                     f"• Max size: {MAX_FILE_MB} MB\n• 🛡 Agent 4 scans it for malicious code\n"
                     "• 🧩 Agent 1 repairs common syntax mistakes\n• 📦 Dependencies are auto-installed\n"
                     "• 🤖 Auto-Fix Assistant heals missing-module crashes\n"
                     "• ♻️ Premium users can update a hosted bot by uploading a changed file with the same bot token\n"
                     "• 🛡 Duplicate identical files are rejected to save server resources\n"
                     "• Premium users can host up to 10 bots\n\n"
                     f"📊 {quota}",
                     reply_markup=inline([[btn("📂 My Bots", "primary", cb="mb:list:0")]]))


@bot.message_handler(func=lambda m: m.text == BTN_DEV)
@safe
def h_dev(m):
    bot.send_message(m.chat.id, "👨‍💻 <b>Developer</b>\n\nHave a problem or a question? Message the developer directly.",
                     reply_markup=inline([[btn("💬 Contact Developer", "primary",
                                               url=f"https://t.me/{DEVELOPER_USERNAME}")]]))


@bot.message_handler(func=lambda m: m.text in HELP_TXT)
@safe
def h_help(m):
    bot.send_message(m.chat.id,
                     f"🛠 <b>Help & Support</b>\n{DIV}\n\n"
                     "1️⃣ Tap <b>📤 Upload File</b> and send your <code>.py</code> bot\n"
                     "2️⃣ Manage it in <b>📂 My Bots</b>: start, stop, logs, auto-restart\n"
                     "3️⃣ Watch live uptime in <b>📊 My Bots Status</b>\n"
                     "4️⃣ Stuck? Open your bot logs and contact support.\n"
                     "5️⃣ More capacity? Use <b>🎁 Refer</b> or <b>💎 Buy Premium</b>\n\n"
                     "<b>Not allowed:</b> malware, token stealers, SMS/call bombers, nested hosting platforms, "
                     "phone scrapers, or code that scans the server / reads other users' files.\n\n"
                     f"📬 Problem or question? Message the admin: @{DEVELOPER_USERNAME}",
                     reply_markup=inline([[btn("💬 Contact Admin", "primary", url=f"https://t.me/{DEVELOPER_USERNAME}")],
                                          [btn("💎 Buy Premium", "success", cb="go:premium"), btn("🎁 Refer & Earn", "primary", cb="go:refer")]]))


@bot.message_handler(func=lambda m: m.text == BTN_PING)
@safe
def h_ping(m):
    t0 = time.time()
    try:
        bot.get_me()
        ms = f"{int((time.time() - t0) * 1000)} ms"
    except Exception:
        ms = "unreachable"
    s = system_stats()
    load = "🟢 Low" if s["ram_pct"] < 60 else "🟡 Medium" if s["ram_pct"] < 85 else "🔴 High"
    live = sum(1 for x in list(procs.values()) if x["thread"].is_alive())
    bot.send_message(m.chat.id,
                     f"⚡ <b>Server Ping</b>\n{DIV}\n"
                     f"📡 Telegram latency: <b>{ms}</b>\n🖥 Server load: {load}\n"
                     f"⏱ Uptime: <b>{fmt_dur(time.time() - START_TIME)}</b>\n🤖 Bots running: <b>{live}</b>\n"
                     f"🐳 Docker: {'✅' if docker_mgr.ok else '❌'}\n"
                     f"{DIV}\n✅ All systems operational",
                     reply_markup=inline([[btn("🔄 Ping again", "success", cb="go:ping")]]))


@bot.message_handler(func=lambda m: m.text == BTN_PRO)
@safe
def h_pro(m):
    bot.send_message(m.chat.id,
                     f"🚀 <b>Pro Features</b>\n{DIV}\n\n"
                     f"💎 Up to <b>{PREMIUM_BOT_LIMIT}</b> concurrent bots\n"
                     "🐳 Isolated Docker containers (private FS, capped CPU/RAM)\n"
                     "🧠 Double RAM allowance per bot\n"
                     "♻️ Priority crash-recovery & Auto-Fix\n"
                     "🧩 Code Fixer + 🛡 Security Scanner on every upload\n"
                     "📊 Live status dashboard\n"
                     "⚡ Instant UPI activation, no waiting\n\n"
                     "Free users get lifetime credits; Premium gives real capacity.",
                     reply_markup=inline([[btn("💎 Upgrade Now", "success", cb="go:premium")],
                                          [btn("🎁 Earn free slots", "primary", cb="go:refer"),
                                           btn("🎁 Refer & Earn", "primary", cb="go:refer")]]))


# ------------------------------ redeem codes ------------------------------
def redeem_intro(chat_id):
    channel = REDEEM_CHANNEL.strip()
    channel_url = f"https://t.me/{channel.lstrip('@')}" if channel.startswith('@') else "https://t.me/"
    channel_label = esc(channel if channel else "Not configured")
    text = (f"🎟️ <b>Redeem Code</b>\n{DIV}\n\n"
            f"Daily redeem codes are posted in: <b>{channel_label}</b>\n"
            "Join the channel and check its latest posts for available codes.\n\n"
            "Enter your redeem code below. Each code can be used by <b>one user only</b>.\n"
            "Agar code already use ho chuka hai, it cannot be redeemed again.")
    markup = inline([[btn("📢 Open Code Channel", "primary", url=channel_url)],
                     [btn("🎟️ Enter Redeem Code", "success", cb="redeem:enter")]])
    bot.send_message(chat_id, text, reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == BTN_REDEEM)
@safe
def h_redeem(m):
    why = blocked(m.from_user.id)
    if why:
        return bot.send_message(m.chat.id, why)
    redeem_intro(m.chat.id)


@safe
def step_redeem_code(m):
    if (m.text or "").strip() == "/cancel":
        return bot.send_message(m.chat.id, "Redeem cancelled.", reply_markup=main_menu(m.from_user.id))
    uid = m.from_user.id
    code = (m.text or "").strip().upper()
    if not code or len(code) > 80 or any(ch.isspace() for ch in code):
        msg = bot.send_message(m.chat.id, "❌ Invalid code format. Please enter the exact redeem code.")
        bot.register_next_step_handler(msg, step_redeem_code)
        return
    # One transaction ensures simultaneous attempts cannot redeem the same code twice.
    with _db_lock:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM redeem_codes WHERE code=? COLLATE NOCASE", (code,)).fetchone()
            if not row:
                con.rollback()
                return bot.send_message(m.chat.id, "❌ Code not found. Please check it and try again.", reply_markup=main_menu(uid))
            if row["used_by"] is not None:
                con.rollback()
                return bot.send_message(m.chat.id, "🚫 This code has already been redeemed by another user. Code pehle hi use ho chuka hai.", reply_markup=main_menu(uid))
            cur = con.execute("UPDATE redeem_codes SET used_by=?, used_username=?, redeemed_at=? WHERE id=? AND used_by IS NULL",
                              (uid, m.from_user.username, utcnow().isoformat(timespec="seconds"), row["id"]))
            if cur.rowcount != 1:
                con.rollback()
                return bot.send_message(m.chat.id, "🚫 This code has already been redeemed. Code pehle hi use ho chuka hai.", reply_markup=main_menu(uid))
            con.execute("UPDATE users SET referral_credits=COALESCE(referral_credits,0)+? WHERE user_id=?",
                        (int(row["credits"]), uid))
            con.commit()
        except Exception:
            con.rollback()
            log.exception("redeem code transaction failed")
            return bot.send_message(m.chat.id, "⚠️ Could not redeem this code right now. Please try again later.")
        finally:
            con.close()
    log_event("redeem", uid=uid, detail=f"code_id={row['id']} credits={row['credits']}")
    bot.send_message(m.chat.id,
                     f"✅ <b>Redeem successful!</b>\nYou received <b>{int(row['credits'])}</b> hosting credit(s).\n"
                     f"Available credits: <b>{free_remaining(uid)}</b>.", reply_markup=main_menu(uid))


@bot.message_handler(func=lambda m: m.text == BTN_PROFILE)
@safe
def h_profile(m):
    uid = m.from_user.id
    why = blocked(uid)
    if why:
        return bot.send_message(m.chat.id, why)
    u = get_user(uid)
    active = bot_count(uid)
    running = rows("SELECT COUNT(*) c FROM bots WHERE user_id=? AND status='running'", (uid,))[0]["c"]
    if is_admin(uid):
        plan = "👑 Admin — God Mode"
        quota_line = f"📊 Active slots: <b>{active}</b> (unlimited)"
    elif is_premium(uid):
        until = datetime.fromisoformat(u["premium_until"])
        plan = f"💎 Premium (until {until:%Y-%m-%d}, {max(0, (until - utcnow()).days)} days left)"
        quota_line = f"📊 Hosted bots: <b>{active}/{premium_cap(uid)}</b>"
    else:
        plan = "🆓 Free"
        quota_line = f"📊 Lifetime free credits: <b>{free_remaining(uid)}</b> remaining (used {u['lifetime_free_used'] or 0}/{free_credit_total(u)})"
    bot.send_message(m.chat.id,
                     f"👤 <b>Your Profile</b>\n{DIV}\n\n"
                     f"🆔 User ID: <code>{uid}</code>\n"
                     f"📦 Plan: {plan}\n"
                     f"🤖 Hosted bots: <b>{active}</b> ({running} running)\n"
                     f"{quota_line}\n"
                     f"🎁 Referral bonus: <b>+{u['extra_slots']}</b>\n"
                     f"📅 Joined: {esc((u['joined_at'] or '')[:10])}",
                     reply_markup=inline([[btn("📂 My Bots", "primary", cb="mb:list:0"),
                                           btn("💎 Upgrade", "success", cb="go:premium")]]))


@bot.message_handler(func=lambda m: m.text == BTN_REFER)
@safe
def h_refer(m):
    why = blocked(m.from_user.id)
    if why:
        return bot.send_message(m.chat.id, why)
    uid = m.from_user.id
    n = rows("SELECT COUNT(*) c FROM referrals WHERE referrer_id=?", (uid,))[0]["c"]
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    bot.send_message(m.chat.id,
                     f"🎁 <b>Refer & Earn</b>\n{DIV}\n\nFor every new user who joins with your link you get "
                     f"<b>+{REFERRAL_BONUS_SLOTS}</b> permanent Premium bot slot (free credit stays 1 per user).\n\n🔗 Your link:\n<code>{link}</code>\n\n"
                     f"✅ Verified referrals: <b>{rows('SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND completed=1', (uid,))[0]['c']}</b>\n"
                     f"🎟 Available credits: <b>{free_remaining(uid)}</b>",
                     reply_markup=inline([[btn("📨 Share Link", "success",
                                               url=f"https://t.me/share/url?url={link}&text=Host%20your%20Telegram%20bots%20free!")]]))


def plans_markup():
    pl = rows("SELECT * FROM plans ORDER BY days")
    return pl, inline([[btn(f"💎 {p['label']} — ₹{p['price']}", "primary", cb=f"plan:{p['id']}")] for p in pl])


@bot.message_handler(func=lambda m: m.text == BTN_PREMIUM)
@safe
def h_premium(m):
    why = blocked(m.from_user.id)
    if why:
        return bot.send_message(m.chat.id, why)
    u = get_user(m.from_user.id)
    status = f"✅ Premium until {esc(u['premium_until'][:10])}" if is_premium(m.from_user.id) else "🎁 Referral-credit plan"
    _, mk = plans_markup()
    bot.send_message(m.chat.id,
                     f"💎 <b>LX Hosting Premium</b>\n{DIV}\n\nStatus: {status}\n"
                     f"Premium gives up to <b>{PREMIUM_BOT_LIMIT}</b> concurrent bot slots.\n\n"
                     "⚡ Instant, automatic UPI payment — scan & go!\n\nChoose a plan 👇", reply_markup=mk)


# ------------------------- upload pipeline + queue ----------------------
upload_q = queue.Queue(maxsize=UPLOAD_QUEUE_MAX)
_overflow = {"n": 0}


def upload_worker():
    while True:
        job = upload_q.get()
        try:
            process_upload(*job)
        except Exception:
            log.exception("upload job crashed")
        finally:
            upload_q.task_done()


def _overflow_run(job):
    _overflow["n"] += 1
    try:
        process_upload(*job)
    except Exception:
        log.exception("overflow upload crashed")
    finally:
        _overflow["n"] -= 1


def dispatch_upload(job):
    def a():
        upload_q.put_nowait(job)
        return True

    def b():
        # No overflow threads: each upload can trigger dependency installs and consume substantial RAM.
        return False

    def c():
        return False
    res, _ = triple("dispatch_upload", a, b, c, default=False)
    return res


@bot.message_handler(content_types=["document"])
@safe
def on_document(m):
    uid = m.from_user.id
    why = blocked(uid)
    if why:
        return bot.reply_to(m, why)
    doc = m.document
    fname = doc.file_name or ""
    if not fname.lower().endswith(".py"):
        msg = bot.reply_to(m, "❌ Only <code>.py</code> files are accepted.")
        return auto_delete(m.chat.id, msg.message_id)
    if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
        msg = bot.reply_to(m, f"❌ File too large (max {MAX_FILE_MB} MB).")
        return auto_delete(m.chat.id, msg.message_id)
    stem = re.sub(r"[^A-Za-z0-9_\-]", "_", fname[:-3])[:40] or "bot"
    exists = rows("SELECT id FROM bots WHERE user_id=? AND name=?", (uid, stem))
    if not exists and bot_count(uid) == 0 and not can_create_new_bot(uid):
        # (users who already host a bot are let through: the file may be a re-upload of the same bot token,
        #  which is a replacement and needs no new credit. process_upload makes the final decision.)
        msg = (CREDIT_GONE_MSG if not is_premium(uid) else "🚫 You've reached your premium slot limit.")
        limit_msg = bot.reply_to(m, msg,
                                 reply_markup=inline([[btn("🎁 Refer", "success", cb="go:refer"),
                                                       btn("💎 Premium", "primary", cb="go:premium")]]))
        return auto_delete(m.chat.id, limit_msg.message_id, delay=20)
    st = bot.reply_to(m, f"⏳ Added to the queue (position #{upload_q.qsize() + 1})...")
    if not dispatch_upload((m, doc, stem, st.message_id)):
        try:
            bot.edit_message_text("🚦 The server is very busy right now. Please try again in a minute.",
                                  m.chat.id, st.message_id)
        except Exception:
            pass


class Progress:
    STAGES = [("scan", "🔄 Scanning file", "1 minute"), ("fix", "🧩 Fixing code", "40 seconds"),
              ("deps", "📦 Installing dependencies", "30 seconds"), ("host", "🚀 Hosting your bot", "15 seconds")]

    def __init__(self, chat, mid):
        self.chat, self.mid, self.last_t, self.last_txt = chat, mid, 0.0, ""

    def show(self, key, note="", force=False):
        keys = [s[0] for s in self.STAGES]
        i = keys.index(key)
        label, eta = self.STAGES[i][1], self.STAGES[i][2]
        marks = " ".join(("✅" if j < i else "⏳" if j == i else "▫️") + " " + self.STAGES[j][1].split(" ", 1)[1].split()[0]
                         for j in range(len(keys)))
        txt = (f"{label}... Estimated time: {eta}.\n{DIV}\n{pbar((i + 1) * 100 // len(keys))} "
               f"{(i + 1) * 100 // len(keys)}%\n{marks}" + (f"\n\n<i>{esc(note)}</i>" if note else ""))
        if txt == self.last_txt or (not force and time.time() - self.last_t < 1.0):
            return
        self.last_txt, self.last_t = txt, time.time()
        try:
            bot.edit_message_text(txt, self.chat, self.mid)
        except Exception as e:
            log.debug("progress edit failed: %s", e)

    def final(self, text, markup=None):
        try:
            bot.edit_message_text(text, self.chat, self.mid, reply_markup=markup)
        except Exception as e:
            log.debug("final edit failed: %s", e)


def download_telegram_file(doc):
    def a():
        fi = bot.get_file(doc.file_id)
        return bot.download_file(fi.file_path)

    def b():
        fi = bot.get_file(doc.file_id)
        r = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fi.file_path}", timeout=NET_TIMEOUT * 2)
        r.raise_for_status()
        return r.content

    def c():
        time.sleep(3)
        return b()
    res, _ = triple("download", a, b, c)
    return res


def decode_source(data):
    res, _ = triple("decode", lambda: data.decode("utf-8-sig"),
                    lambda: data.decode("utf-8", errors="replace"), None)
    return res


def process_upload(m, doc, stem, mid):
    uid, chat = m.from_user.id, m.chat.id
    stage = "init"
    prog = Progress(chat, mid)
    fix_notes = []
    new_bot_id, credit_claimed, hosted_ok, replaced = None, False, False, False

    def reject(reason):
        prog.final(f"🚫 <b>Rejected</b>\n{DIV}\n\nThis file breaks the hosting rules: <i>{esc(reason)}</i>\n\n"
                   "Token stealers, server-scanning code, SMS/call bombers, nested hosting platforms, "
                   "phone scrapers and dynamic-exec calls are not allowed.\n\n"
                   "🗑 The file was discarded and nothing was kept on the server.")
        auto_delete(chat, mid, delay=15)
        log.warning("Rejected upload from %s: %s", uid, reason)
        log_event("reject", uid, None, f"{doc.file_name}: {reason}")
        gc.collect()      # rejected code lived only in RAM (scan runs BEFORE any disk write) -> release it now
        notify_admins(f"🚫 Rejected upload\nUser: <code>{uid}</code> "
                      f"@{esc(m.from_user.username)}\nFile: {esc(doc.file_name)}\nReason: {esc(reason)}")

    try:
        stage = "download"
        prog.show("scan", "Downloading your file…", force=True)
        data = download_telegram_file(doc)
        if data is None:
            return prog.final("⚠️ Couldn't download your file from Telegram. Please send it again.")
        src = decode_source(data)
        if src is None:
            return prog.final("❌ File must be UTF-8 text Python code.")

        stage = "scan"
        prog.show("scan", "Agent 4 is checking for malicious code…", force=True)
        ok, reason, syntax_bad = agent4_scan(src)
        if not ok and not syntax_bad:
            return reject(reason)

        stage = "fix"
        prog.show("fix", "Agent 1 is checking code health…", force=True)
        if syntax_bad:
            new_src, notes = agent1_fix(src)
            fixed_ok = False
            if notes:
                ok2, reason2, still_bad = agent4_scan(new_src)
                if ok2:
                    src, fix_notes, fixed_ok = new_src, notes, True
                elif not still_bad:
                    return reject(reason2)
            if not fixed_ok:
                log_event("reject", uid, None, f"{doc.file_name}: unfixable syntax")
                prog.final(f"❌ <b>Couldn't host this file</b>\n{DIV}\n\n💡 {agent2_explain(reason)}\n\n"
                           f"<code>{esc(reason)}</code>")
                return auto_delete(chat, mid, delay=30)
        payload = src.encode("utf-8")

        stage = "save-file"
        with ulock(uid):
            # Exact source duplicates are rejected before replacing files or installing packages.
            incoming_hash = hashlib.sha256(payload).hexdigest()
            for existing in rows("SELECT id,name FROM bots WHERE user_id=?", (uid,)):
                existing_path = os.path.join(HOSTED_DIR, str(uid), existing["name"], existing["name"] + ".py")
                try:
                    with open(existing_path, "rb") as ef:
                        if hashlib.sha256(ef.read(MAX_FILE_MB * 1024 * 1024 + 1)).hexdigest() == incoming_hash:
                            prog.final("♻️ <b>Duplicate file detected</b>\n\nThis exact file is already hosted. "
                                       "No new process or files were created. Upload changed code only when you need an update.")
                            log_event("duplicate_upload", uid, existing["id"], doc.file_name or stem)
                            return auto_delete(chat, mid, delay=20)
                except OSError:
                    continue

            hashes = token_hashes(src)
            targets, seen = [], set()

            def _collect(rs):
                for r in rs:
                    if r["id"] not in seen:
                        seen.add(r["id"])
                        targets.append(r)
            if hashes:      # same bot token already hosted by this user (under any file name)
                cond = " OR ".join("token_hash LIKE ?" for _ in hashes)
                _collect(rows(f"SELECT * FROM bots WHERE user_id=? AND ({cond}) ORDER BY id",
                              (uid, *[f"% {h} %" for h in hashes])))
            _collect(rows("SELECT * FROM bots WHERE user_id=? AND name=? ORDER BY id", (uid, stem)))
            replaced = bool(targets)
            if replaced and not (is_admin(uid) or is_premium(uid)):
                prog.final("💎 <b>Bot update is a Premium feature</b>\n\n"
                           "Free hosting supports your first deployment. Upgrade to Premium to upload changed code "
                           "for an already-hosted bot token while keeping the same bot identity.",
                           inline([[btn("💎 Buy Premium", "success", cb="go:premium")]]))
                return auto_delete(chat, mid, delay=25)

            if not replaced:                 # brand-new bot -> needs a slot / the single free credit
                if is_admin(uid) or is_premium(uid):
                    if not can_create_new_bot(uid):
                        prog.final("🚫 You've reached your premium slot limit.")
                        return auto_delete(chat, mid, delay=15)
                else:
                    if not claim_free_credit(uid):
                        prog.final(CREDIT_GONE_MSG)
                        return auto_delete(chat, mid, delay=15)
                    credit_claimed = True

            # ---- GARBAGE COLLECTION: old process/container dies, old files are DELETED first ----
            for t in targets:
                stop_bot(t["id"])
                wipe_bot_files(t["user_id"], t["name"], t["id"])
            for t in targets[1:]:
                run("DELETE FROM bots WHERE id=?", (t["id"],))
            thash = pack_hashes(hashes)
            stage = "database"
            if targets:
                bot_id = targets[0]["id"]
                run("UPDATE bots SET name=?, token_hash=?, status='stopped', tg_username=NULL, "
                    "runtime='subprocess' WHERE id=?", (stem, thash, bot_id))
            else:
                bot_id = run("INSERT INTO bots(user_id,name,status,autorestart,created,token_hash) "
                             "VALUES(?,?,?,?,?,?)", (uid, stem, "stopped", 1, utcnow().isoformat(), thash))
                new_bot_id = bot_id
            public_id = ensure_public_bot_id(bot_id)
            stage = "save-file"
            d = prepare_dir(uid, stem)
            write_user_file(d, stem + ".py", payload)

        ok_p, bad_p, warns = [], [], []
        stage = "dependencies"
        try:
            pkgs = packages_for(src, d)
            if pkgs:
                prog.show("deps", f"Installing {len(pkgs)} package(s)…", force=True)
                try:
                    write_user_file(d, "requirements.txt", ("\n".join(pkgs) + "\n").encode())
                except Exception:
                    log.exception("could not write requirements.txt (continuing)")
                ok_p, bad_p = pip_install(d, pkgs, lambda i, n, p: prog.show("deps", f"({i}/{n}) {p}"))
        except Exception:
            log.exception("dependency step failed (continuing)")
            warns.append("dependency installation hit an error; check the bot logs")

        stage = "start-bot"
        prog.show("host", "Booting up the instance…", force=True)
        if not start_bot(bot_id):
            if new_bot_id is not None:
                # a brand-new bot that never started is NOT a successful hosting -> roll everything back
                raise RuntimeError("the bot process could not be started")
            warns.append("could not start the bot automatically; press Start in My Bots")
        hosted_ok = True

        rt_note = ""
        if is_admin(uid) or is_premium(uid):
            rt_note = "\n🖥 Running in low-RAM subprocess mode (Docker disabled)."

        public_id = ensure_public_bot_id(bot_id)
        status_url = f"{PUBLIC_BASE_URL}/status/{public_id}" if PUBLIC_BASE_URL else f"/status/{public_id}"
        text = f"✅ <b>{esc(stem)}</b> uploaded and running!\n🆔 Bot ID: <code>{esc(public_id)}</code>\n🌐 Status: <code>{esc(status_url)}</code>"
        if replaced:
            text += "\n♻️ Replaced your previous version — the old process and files were deleted."
        if credit_claimed:
            text += "\n🎟 Your 1 free lifetime credit is now used (it is never refunded)."
        if fix_notes:
            text += f"\n🧩 Auto-fixed: <i>{esc(', '.join(fix_notes))}</i>"
        if ok_p:
            text += f"\n📦 Installed: <code>{esc(', '.join(ok_p))}</code>"
        if bad_p:
            text += f"\n⚠️ Could not install: <code>{esc(', '.join(bad_p))}</code>"
        for w in warns:
            text += f"\n⚠️ {esc(w)}"
        text += rt_note
        prog.final(text, inline([[btn("📂 Manage", "primary", cb=f"mb:view:{bot_id}"),
                                  btn("📜 Logs", cb=f"mb:logs:{bot_id}")]]))
        send_sticker_safe(chat, STICKER_HOSTED)
        log_event("upload", uid, bot_id, f"{stem}" + (f" (fixed: {', '.join(fix_notes)})" if fix_notes else ""))

        uname = lookup_username(src)
        if uname is not None:
            run("UPDATE bots SET tg_username=? WHERE id=?", (uname, bot_id))
        if NOTIFY_ADMIN_ON_DEPLOY:
            notify_admin_upload(m, doc, payload, uname if uname and uname != "-" else "Unknown", fix_notes)
    except Exception:
        tb = traceback.format_exc()
        log.error("UPLOAD FAILED | stage=%s user=%s file=%s\n%s", stage, uid, doc.file_name, tb)
        log_event("error", uid, None, f"upload failed at {stage}")
        if not hosted_ok:      # failed BEFORE the bot ran: leave nothing behind and give the claimed credit back
            try:
                if new_bot_id is not None:
                    nb = get_bot(new_bot_id)
                    if nb:
                        delete_bot(nb)
                if credit_claimed:
                    release_free_credit(uid)
            except Exception:
                log.exception("upload rollback failed")
        prog.final(f"⚠️ Upload failed at step <b>{esc(stage)}</b>. The error was logged for the admin — "
                   "please try again in a moment.")
        auto_delete(chat, mid, delay=20)
        if DEBUG_ERRORS_TO_ADMIN:
            notify_admins(f"🐞 <b>Upload error</b> (step: {esc(stage)})\nUser: <code>{uid}</code>\n"
                          f"<pre>{esc(tb[-1500:])}</pre>")
    finally:
        gc.collect()       # free the uploaded source / payload from RAM right away


def notify_admin_upload(m, doc, file_bytes, hosted_username, fix_notes):
    uploader = f"@{m.from_user.username}" if m.from_user.username else "(no username)"
    caption = ("📥 <b>New Bot Hosted</b>\n\n"
               f"👤 Uploader: <code>{m.from_user.id}</code> ({esc(uploader)})\n"
               f"📄 File: {esc(doc.file_name)}\n"
               f"🤖 Hosted Bot: {esc(hosted_username)}\n"
               + (f"🧩 Auto-fixed: {esc(', '.join(fix_notes))}\n" if fix_notes else "") +
               "📌 Status: ✅ Successfully Hosted & Running")

    def fanout():
        for aid in ADMIN_CHAT_IDS:
            triple(f"notify_admin_upload:{aid}",
                   lambda aid=aid: bot.send_document(aid, (doc.file_name, file_bytes), caption=caption),
                   lambda aid=aid: bot.send_message(aid, caption),
                   lambda: log.info("admin notification skipped"))
    threading.Thread(target=fanout, daemon=True).start()


# ------------------------------ callbacks ------------------------------
@bot.callback_query_handler(func=lambda c: True)
@safe
def on_cb(c):
    uid, data = c.from_user.id, c.data or ""
    chat, mid = c.message.chat.id, c.message.message_id
    if data.startswith("adm:"):
        return admin_cb(c)
    if data == "joined":
        register(c.from_user)
        u = get_user(uid)
        if u["banned"]:
            return bot.answer_callback_query(c.id, blocked(uid), show_alert=True)
        if is_admin(uid):
            run("UPDATE users SET confirmed=1 WHERE user_id=?", (uid,))
            bot.answer_callback_query(c.id, "👑 Admin — access granted!")
            try:
                bot.delete_message(chat, mid)
            except Exception:
                pass
            return send_menu(chat, c.from_user.first_name)
        missing = missing_channels(uid)
        if missing:
            bot.answer_callback_query(c.id, "❌ You still need to join: " + ", ".join(missing), show_alert=True)
            try:
                bot.edit_message_reply_markup(chat, mid, reply_markup=join_markup(missing))
            except Exception:
                pass
            return
        run("UPDATE users SET confirmed=1 WHERE user_id=?", (uid,))
        award_verified_referral(uid)
        bot.answer_callback_query(c.id, "✅ Access granted!")
        try:
            bot.delete_message(chat, mid)
        except Exception:
            pass
        send_sticker_safe(chat, STICKER_START)
        return send_menu(chat, c.from_user.first_name)
    why = blocked(uid)
    if why:
        return bot.answer_callback_query(c.id, why, show_alert=True)
    parts = data.split(":")
    if data == "redeem:enter":
        bot.answer_callback_query(c.id)
        msg = bot.send_message(chat, "🎟️ <b>Enter Redeem Code</b>\nSend your code in the next message. Use /cancel to stop.")
        bot.register_next_step_handler(msg, step_redeem_code)
        return
    if data == "st:refresh":
        bot.answer_callback_query(c.id, "Refreshed")
        t, mk = status_view(uid)
        return bot.edit_message_text(t, chat, mid, reply_markup=mk)
    if data == "go:ping":
        t0 = time.time()
        try:
            bot.get_me()
            bot.answer_callback_query(c.id, f"⚡ {int((time.time() - t0) * 1000)} ms — all systems operational",
                                      show_alert=True)
        except Exception:
            bot.answer_callback_query(c.id, "⚠️ Telegram API slow right now", show_alert=True)
        return
    if data == "go:refer":
        bot.answer_callback_query(c.id)
        link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
        msg = bot.send_message(chat, f"🎁 Your referral link:\n<code>{link}</code>")
        return auto_delete(chat, msg.message_id, delay=20)
    if data == "go:premium":
        bot.answer_callback_query(c.id)
        _, mk = plans_markup()
        return bot.send_message(chat, "💎 <b>Premium plans</b>", reply_markup=mk)
    if parts[0] == "plan":
        p = rows("SELECT * FROM plans WHERE id=?", (int(parts[1]),))
        if not p:
            return bot.answer_callback_query(c.id, "Plan no longer exists.", show_alert=True)
        p = p[0]
        bot.answer_callback_query(c.id, "💳 Generating secure UPI payment link...")
        status = bot.send_message(chat, "💳 Contacting payment gateway...")
        animate(chat, status.message_id, ["💳 Contacting payment gateway...", "🧾 Generating UPI order..."], delay=1.0)
        order = fg_create_order(p["price"], customer_name=f"{uid}:{c.from_user.username or c.from_user.first_name}")
        if not order:
            try:
                bot.edit_message_text("⚠️ Payment gateway is unavailable right now. Please try again shortly.",
                                      chat, status.message_id)
            except Exception:
                pass
            return
        run("INSERT OR REPLACE INTO orders(user_id,order_id,plan_id,amount,status,created) VALUES(?,?,?,?,?,?)",
            (uid, order["order_id"], p["id"], float(order.get("payable_amount", p["price"])),
             "pending", utcnow().isoformat()))
        try:
            bot.delete_message(chat, status.message_id)
        except Exception:
            pass
        caption = (f"💳 <b>Instant UPI Payment</b>\n{DIV}\n\n"
                   f"💎 Plan: <b>{esc(p['label'])}</b>\n💰 Amount: <b>₹{esc(order.get('payable_amount', p['price']))}</b>\n"
                   f"🧾 Order ID: <code>{esc(order['order_id'])}</code>\n\n"
                   "Scan the QR with any UPI app (GPay, PhonePe, Paytm) or tap the button below.\n"
                   "⚡ Verifies automatically within seconds of payment!")
        pay_msg = send_payment_card(chat, order, caption,
                                    inline([[btn("🌐 Open Checkout", "primary", url=order["checkout_url"])],
                                            [btn("❌ Cancel", "danger", cb=f"pay:cancel:{order['order_id']}")]]))
        if pay_msg:
            threading.Thread(target=poll_payment, args=(uid, chat, pay_msg.message_id, order["order_id"], p),
                             daemon=True).start()
        return
    if parts[0] == "pay" and parts[1] == "cancel":
        order_id = parts[2]
        run("UPDATE orders SET status='cancelled' WHERE order_id=? AND user_id=? AND status='pending'", (order_id, uid))
        bot.answer_callback_query(c.id, "Cancelled.")
        try:
            bot.edit_message_caption(caption="❌ <b>Payment cancelled.</b>", chat_id=chat, message_id=mid, reply_markup=None)
        except Exception:
            try:
                bot.edit_message_text("❌ <b>Payment cancelled.</b>", chat, mid)
            except Exception:
                pass
        return
    if parts[0] == "ul":
        bot.answer_callback_query(c.id)
        return show_bots(chat, uid)
    if parts[0] == "mb":
        act, bid = parts[1], int(parts[2])
        if act == "list":
            bot.answer_callback_query(c.id)
            return show_bots(chat, uid, edit_mid=mid)
        b = get_bot(bid, uid)
        if not b:
            return bot.answer_callback_query(c.id, "Bot not found.", show_alert=True)
        if act in ("view", "refresh"):
            bot.answer_callback_query(c.id, "Status refreshed" if act == "refresh" else None)
            t, mk = panel(b)
            return bot.edit_message_text(t, chat, mid, reply_markup=mk)
        if act == "start":
            start_bot(bid)
            bot.answer_callback_query(c.id, "▶️ Starting...")
        elif act == "stop":
            stop_bot(bid)
            bot.answer_callback_query(c.id, "⏹ Stopped")
        elif act == "restart":
            stop_bot(bid)
            start_bot(bid)
            bot.answer_callback_query(c.id, "🔄 Restarting...")
        elif act == "ar":
            run("UPDATE bots SET autorestart=1-autorestart WHERE id=?", (bid,))
            bot.answer_callback_query(c.id, "Updated")
        elif act == "logs":
            bot.answer_callback_query(c.id)
            t = tail(log_path(bid)).strip() or "(no output yet)"
            msg = bot.send_message(chat, f"📜 <b>Logs — {esc(b['name'])}</b>\n<pre>{esc(t[-3500:])}</pre>")
            return auto_delete(chat, msg.message_id, delay=30)
        elif act == "delq":
            bot.answer_callback_query(c.id)
            return bot.edit_message_text(f"🗑 Delete <b>{esc(b['name'])}</b> and all its files?\n\n"
                                         "⚠️ This will NOT refund a lifetime free credit if it used one.", chat, mid,
                                         reply_markup=inline([[btn("✅ Yes, delete", "danger", cb=f"mb:del:{bid}"),
                                                               btn("❌ Cancel", cb=f"mb:view:{bid}")]]))
        elif act == "del":
            delete_bot(b)
            bot.answer_callback_query(c.id, "Deleted")
            return show_bots(chat, uid, edit_mid=mid)
        time.sleep(0.5)
        t, mk = panel(get_bot(bid, uid))
        bot.edit_message_text(t, chat, mid, reply_markup=mk)


# ============================ [5] ADMIN PANEL (Massive, multi-level) ============================
def admin_main_menu():
    return inline([
        [btn("📊 Dashboard", "primary", cb="adm:stats")],
        [btn("🖥 Server Control", "primary", cb="adm:srv"), btn("🐳 Docker Panel", "primary", cb="adm:dk")],
        [btn("🤖 Ultimate Bot Manager", "success", cb="adm:bm:0")],
        [btn("👥 User Manager", "primary", cb="adm:um"), btn("🛡 Security Panel", "danger", cb="adm:sec")],
        [btn("⚙️ Settings", "primary", cb="adm:set"), btn("📋 Activity Log", "primary", cb="adm:evt")],
        [btn("💰 Payments", "success", cb="adm:pay"), btn("🎬 Welcome Media", "primary", cb="adm:wm")],
        [btn("🎟️ Redeem Codes", "success", cb="adm:redeem")],
        [btn("🧠 AI Advisor", "primary", cb="adm:adv"), btn("🔧 Maintenance", "primary", cb="adm:maint")],
        [btn("📢 Broadcast", "primary", cb="adm:bc")],
        [btn("🔄 Update Bot Code", "danger", cb="adm:ota")],
        [btn("🔄 Restart All", "danger", cb="adm:ra"), btn("⏹ Stop All", "danger", cb="adm:sa")],
    ])


def admin_srv_menu():
    return inline([
        [btn("🖥 Server Health", "primary", cb="adm:srhealth")],
        [btn("🐳 Docker Status", "primary", cb="adm:dk")],
        [btn("🧹 Kill Frozen Now", "danger", cb="adm:kf")],
        [btn("💾 Backup DB", "primary", cb="adm:bk")],
        [btn("🔄 Restart All Hosted", "danger", cb="adm:ra"), btn("⏹ Stop All Hosted", "danger", cb="adm:sa")],
        [btn("📈 Top Bots by RAM", "primary", cb="adm:top")],
        [btn("⬅️ Admin menu", cb="adm:menu")],
    ])


def admin_um_menu():
    return inline([
        [btn("🔍 Lookup User", "primary", cb="adm:usr")],
        [btn("👥 Browse Users", "primary", cb="adm:ulist:0")],
        [btn("➕ Grant Premium", "success", cb="adm:addp"), btn("➖ Remove Premium", "danger", cb="adm:remp")],
        [btn("🎁 Give Bonus Slots", "success", cb="adm:bonus")],
        [btn("🚫 Ban", "danger", cb="adm:ban"), btn("✅ Unban", "success", cb="adm:unban")],
        [btn("⬅️ Admin menu", cb="adm:menu")],
    ])


def admin_sec_menu():
    since = (utcnow() - timedelta(hours=24)).isoformat()
    rejects = rows("SELECT COUNT(*) c FROM events WHERE kind='reject' AND ts>?", (since,))[0]["c"]
    kills = rows("SELECT COUNT(*) c FROM events WHERE kind='kill' AND ts>?", (since,))[0]["c"]
    return inline([
        [btn(f"📋 Recent Rejections ({rejects})", "primary", cb="adm:secrej")],
        [btn(f"🛡 Recent Kills ({kills})", "primary", cb="adm:seckil")],
        [btn("🚫 Blocked Patterns (info)", "primary", cb="adm:secinfo")],
        [btn("📋 Full Activity Log", "primary", cb="adm:evt")],
        [btn("⬅️ Admin menu", cb="adm:menu")],
    ])


def admin_set_menu():
    return inline([
        [btn("💰 Edit Plan Prices", "primary", cb="adm:prices")],
        [btn("🎬 Welcome Media", "primary", cb="adm:wm")],
        [btn("⚙️ Force Docker Reconnect", "primary", cb="adm:dkrecon")],
        [btn("📢 Broadcast", "primary", cb="adm:bc")],
        [btn("⬅️ Admin menu", cb="adm:menu")],
    ])


def admin_maint_menu():
    return inline([
        [btn("🔄 Restart All Hosted Bots", "danger", cb="adm:ra")],
        [btn("⏹ Stop All Hosted Bots", "danger", cb="adm:sa")],
        [btn("🧹 Kill Frozen Now", "danger", cb="adm:kf")],
        [btn("💾 Backup DB", "primary", cb="adm:bk")],
        [btn("🧬 Recover Orphaned Bots", "primary", cb="adm:recover")],
        [btn("🧹 Deep Clean Storage", "danger", cb="adm:clean")],
        [btn("🔄 Update Bot Code", "danger", cb="adm:ota")],
        [btn("⬅️ Admin menu", cb="adm:menu")],
    ])


def admin_pay_menu():
    paid = rows("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM orders WHERE status='paid'")[0]
    pend = rows("SELECT COUNT(*) c FROM orders WHERE status='pending'")[0]["c"]
    return inline([
        [btn(f"💵 {paid['c']} paid • ₹{paid['s']:.0f}", "success", cb="adm:paylist:paid:0")],
        [btn(f"⏳ {pend} pending", "primary", cb="adm:paylist:pending:0")],
        [btn("⬅️ Admin menu", cb="adm:menu")],
    ])


@bot.message_handler(func=lambda m: m.text == BTN_ADMIN and is_admin(m.from_user.id))
@safe
def h_admin(m):
    bot.send_message(m.chat.id, f"🛠 <b>Admin Panel</b>\n{DIV}\n\nFull control over users, bots, Docker and the server 👇",
                     reply_markup=admin_main_menu())


def ask(chat_id, text, handler):
    msg = bot.send_message(chat_id, text + "\n\n<i>Send /cancel to abort.</i>")
    bot.register_next_step_handler(msg, handler)


def cancelled(m):
    if (m.text or "").strip() == "/cancel":
        bot.send_message(m.chat.id, "Cancelled.", reply_markup=admin_main_menu())
        return True
    return False


def parse_uid(m):
    t = (m.text or "").strip().split()
    if t and t[0].lstrip("-").isdigit():
        return int(t[0]), t[1:]
    bot.send_message(m.chat.id, "❌ Send a numeric user ID.")
    return None, None


def adm_edit(c, text, markup=None):
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=markup)
    except telebot.apihelper.ApiTelegramException as e:
        if "not modified" not in str(e):
            bot.send_message(c.message.chat.id, text, reply_markup=markup)
    except Exception:
        bot.send_message(c.message.chat.id, text, reply_markup=markup)


@safe
def step_price(m, plan_id):
    if cancelled(m):
        return
    t = (m.text or "").strip()
    if not t.isdigit():
        return bot.send_message(m.chat.id, "❌ Price must be a number.", reply_markup=admin_main_menu())
    run("UPDATE plans SET price=? WHERE id=?", (int(t), plan_id))
    bot.send_message(m.chat.id, f"✅ Price updated to ₹{t}.", reply_markup=admin_main_menu())


@safe
def step_addp(m):
    if cancelled(m):
        return
    uid, rest = parse_uid(m)
    if uid is None:
        return
    if not rest or not rest[0].isdigit():
        return bot.send_message(m.chat.id, "❌ Format: <code>user_id days</code>", reply_markup=admin_main_menu())
    if not get_user(uid):
        return bot.send_message(m.chat.id, "❌ User not found (they must /start the bot first).", reply_markup=admin_main_menu())
    until = activate_premium(uid, int(rest[0]))
    bot.send_message(m.chat.id, f"✅ Premium for <code>{uid}</code> until {until:%Y-%m-%d}.", reply_markup=admin_main_menu())
    safe_send(uid, f"🎉 <b>Premium activated!</b>\nValid until <b>{until:%Y-%m-%d}</b>. "
                   f"You can now host up to {PREMIUM_BOT_LIMIT} bots, .")
    send_sticker_safe(uid, STICKER_PREMIUM)


@safe
def step_remp(m):
    if cancelled(m):
        return
    uid, _ = parse_uid(m)
    if uid is None:
        return
    run("UPDATE users SET premium_until=NULL WHERE user_id=?", (uid,))
    bot.send_message(m.chat.id, f"✅ Premium removed from <code>{uid}</code>.", reply_markup=admin_main_menu())


@safe
def step_bonus(m):
    if cancelled(m):
        return
    uid, rest = parse_uid(m)
    if uid is None:
        return
    if not rest or not rest[0].lstrip("-").isdigit():
        return bot.send_message(m.chat.id, "❌ Format: <code>user_id amount</code>", reply_markup=admin_main_menu())
    amount = int(rest[0])
    run("UPDATE users SET extra_slots=MAX(0, extra_slots + ?) WHERE user_id=?", (amount, uid))
    bot.send_message(m.chat.id, f"✅ Bonus slots for <code>{uid}</code>: {amount:+d}.", reply_markup=admin_main_menu())
    try:
        safe_send(uid, f"🎁 Admin adjusted your permanent bot slots by {amount:+d}.")
    except Exception:
        pass


@safe
def step_ban(m):
    if cancelled(m):
        return
    uid, _ = parse_uid(m)
    if uid is None:
        return
    if is_admin(uid):
        return bot.send_message(m.chat.id, "❌ You can't ban an admin.", reply_markup=admin_main_menu())
    if not get_user(uid):
        return bot.send_message(m.chat.id, "❌ User not found.", reply_markup=admin_main_menu())
    ban_user(uid)
    log_event("ban", uid, None, "by admin")
    bot.send_message(m.chat.id, f"🚫 <code>{uid}</code> banned and all their bots stopped.", reply_markup=admin_main_menu())
    safe_send(uid, f"🚫 Your account has been suspended. Contact @{DEVELOPER_USERNAME}.")


@safe
def step_unban(m):
    if cancelled(m):
        return
    uid, _ = parse_uid(m)
    if uid is None:
        return
    run("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    bot.send_message(m.chat.id, f"✅ <code>{uid}</code> unbanned.", reply_markup=admin_main_menu())


@safe
def step_broadcast(m):
    if cancelled(m):
        return
    ids = [r["user_id"] for r in rows("SELECT user_id FROM users WHERE banned=0")]
    bot.send_message(m.chat.id, f"📢 Broadcasting to {len(ids)} users...")

    def work():
        ok = fail = 0
        for i in ids:
            try:
                bot.copy_message(i, m.chat.id, m.message_id)
                ok += 1
            except Exception:
                fail += 1
            time.sleep(0.05)
        bot.send_message(m.chat.id, f"📢 Broadcast done.\n✅ Sent: {ok}\n❌ Failed: {fail}",
                         reply_markup=admin_main_menu())
    threading.Thread(target=work, daemon=True).start()


@safe
def step_user_lookup(m):
    if cancelled(m):
        return
    uid, _ = parse_uid(m)
    if uid is None:
        return
    u = get_user(uid)
    if not u:
        return bot.send_message(m.chat.id, "❌ User not found.", reply_markup=admin_main_menu())
    bl = rows("SELECT * FROM bots WHERE user_id=? ORDER BY id", (uid,))
    icon = {"running": "🟢", "stopped": "🔴", "crashed": "⚠️"}
    lines = [f"{icon.get(b['status'], '🔴')} {esc(b['name'])} "
             f"{esc(b['tg_username'] if b['tg_username'] not in (None, '-') else '')} "
             f"[{(b['runtime'] or 'subprocess')[:3]}]" for b in bl]
    bot.send_message(m.chat.id,
                     f"👤 <b>User {uid}</b> {'🚫 BANNED' if u['banned'] else ''}\n{DIV}\n"
                     f"Name: {esc(u['first_name'])} (@{esc(u['username'])})\n"
                     f"Plan: {'💎 until ' + esc(u['premium_until'][:10]) if is_premium(uid) else ('👑 Admin' if is_admin(uid) else '🆓 Free')}\n"
                     f"Free credits left: {free_remaining(uid)} • Bonus slots: {u['extra_slots']}\n"
                     f"Joined: {esc((u['joined_at'] or '')[:10])}\n\n🤖 Bots ({len(bl)}):\n" + ("\n".join(lines) or "—"),
                     reply_markup=admin_main_menu())


@safe
def step_welcome(m):
    if cancelled(m):
        return
    if m.content_type not in ("video", "animation"):
        return bot.send_message(m.chat.id, "❌ Please send a <b>video</b> or <b>GIF/animation</b> "
                                           "(not as a file).", reply_markup=admin_main_menu())
    obj = m.video if m.content_type == "video" else m.animation
    set_setting("welcome_media", json.dumps({"type": m.content_type, "file_id": obj.file_id}))
    ratio = (obj.width / obj.height) if getattr(obj, "width", 0) and getattr(obj, "height", 0) else None
    note = ""
    if ratio and not (1.6 <= ratio <= 1.9):
        note = f"\n⚠️ Ratio is {ratio:.2f}:1 — YouTube-style 16:9 is about 1.78:1."
    bot.send_message(m.chat.id, "✅ Welcome media saved. New users will see it with the /start message." + note,
                     reply_markup=admin_main_menu())


# ===================== OTA CODE UPDATE (admin only) =====================
# Detached watchdog: survives os.execl(). If the NEW code never reports "healthy" (flag file written in
# main()) within the wait time, it restores the previous script and relaunches it - so a bad update
# can never lock you out of a server you can't SSH into.
OTA_WATCHDOG_SRC = r'''
import os, sys, time, shutil, signal, subprocess
script, backup, ok_flag, rb_flag = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
wait, pid = int(sys.argv[5]), int(sys.argv[6])
t0 = time.time()
while time.time() - t0 < wait:
    if os.path.exists(ok_flag):
        sys.exit(0)
    time.sleep(2)
try:
    shutil.copyfile(backup, script)
    with open(rb_flag, "w") as f:
        f.write(str(time.time()))
except Exception:
    sys.exit(1)
try:
    os.kill(pid, signal.SIGKILL)
except Exception:
    pass
time.sleep(2)
subprocess.Popen([sys.executable, script], cwd=os.path.dirname(script), start_new_session=True,
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
'''

_lock_fh = None


def acquire_instance_lock():
    """Only ONE copy of this script may run at a time (prevents double polling after restarts)."""
    global _lock_fh
    if fcntl is None:
        return True
    try:
        os.makedirs(PRIV_DIR, exist_ok=True)
        fh = open(LOCK_PATH, "w")
    except OSError:
        return True
    for _ in range(8):
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.write(str(os.getpid()))
            fh.flush()
            _lock_fh = fh
            return True
        except OSError:
            time.sleep(1)
    return False


def _carry_over_secrets(src):
    """If the uploaded script has an EMPTY BOT_TOKEN / FAM_GATEWAY_API_KEY, fill it with the running value,
    so you can upload a 'clean' script without losing your secrets."""
    carried = []
    for var, val in (("BOT_TOKEN", BOT_TOKEN), ("FAM_GATEWAY_API_KEY", FAM_GATEWAY_API_KEY)):
        pat = re.compile(r'^(' + var + r'\s*=\s*)(""|\'\')(\s*(#.*)?)$', re.M)
        if val and pat.search(src):
            src = pat.sub(lambda m: m.group(1) + json.dumps(val) + m.group(3), src, count=1)
            carried.append(var)
    return src, carried


def apply_ota_update(data):
    """Validate the new script, keep ONE rolling backup of the current one, then atomically overwrite
    this script file. Raises on any problem BEFORE touching the live file."""
    src = data.decode("utf-8-sig")
    src, carried = _carry_over_secrets(src)
    compile(src, "<ota-upload>", "exec")                     # syntax check (SyntaxError -> rejected)
    if "def main(" not in src or "__main__" not in src:
        raise ValueError("this does not look like the hosting bot (no main() entry point found)")
    new_bytes = src.encode("utf-8")
    os.makedirs(PRIV_DIR, exist_ok=True)
    shutil.copyfile(SCRIPT_PATH, OTA_BACKUP_PATH)           # rolling backup (overwritten each update)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SCRIPT_PATH), prefix=".lx_ota_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(new_bytes)
            f.flush()
            os.fsync(f.fileno())
        try:
            shutil.copymode(SCRIPT_PATH, tmp)
        except OSError:
            pass
        os.replace(tmp, SCRIPT_PATH)                        # atomic overwrite of ourselves
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return len(new_bytes), carried


def restart_self(chat_id=None):
    """Replace this process with the freshly written script (os.execl)."""
    deadline = time.time() + 20                              # let queued uploads finish first
    while upload_q.unfinished_tasks and time.time() < deadline:
        time.sleep(0.5)
    log.warning("OTA: restarting into the new code")
    set_setting("ota_notify", json.dumps({"chat": chat_id, "ts": time.time()}))
    for p in (OTA_OK_FLAG, OTA_RB_FLAG):
        try:
            os.remove(p)
        except OSError:
            pass
    stop_all_runtimes_keep_db()                              # no orphan children / duplicate pollers
    try:
        subprocess.Popen([sys.executable, "-c", OTA_WATCHDOG_SRC, SCRIPT_PATH, OTA_BACKUP_PATH, OTA_OK_FLAG,
                          OTA_RB_FLAG, str(OTA_HEALTH_WAIT_SEC), str(os.getpid())],
                         start_new_session=True, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        log.exception("could not start the OTA watchdog (continuing without auto-rollback)")
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass
    try:
        os.execl(sys.executable, sys.executable, SCRIPT_PATH, *sys.argv[1:])
    except Exception:
        log.exception("os.execl failed, trying a fresh process instead")
        try:
            subprocess.Popen([sys.executable, SCRIPT_PATH, *sys.argv[1:]], cwd=BASE_DIR, start_new_session=True)
        finally:
            os._exit(0)


@safe
def step_ota_update(m):
    if not is_admin(m.from_user.id):
        return
    if m.content_type == "text" and cancelled(m):
        return
    doc = m.document if m.content_type == "document" else None
    if not doc or not (doc.file_name or "").lower().endswith(".py"):
        msg = bot.send_message(m.chat.id, "❌ Please send the new script as a <code>.py</code> <b>document</b> "
                                          "(or /cancel).")
        return bot.register_next_step_handler(msg, step_ota_update)
    if doc.file_size and doc.file_size > OTA_MAX_MB * 1024 * 1024:
        return bot.send_message(m.chat.id, f"❌ File too large (max {OTA_MAX_MB} MB).", reply_markup=admin_main_menu())
    st = bot.send_message(m.chat.id, "⬇️ Downloading the new code…")
    data = download_telegram_file(doc)
    if not data:
        return bot.edit_message_text("❌ Couldn't download the file from Telegram. Nothing was changed.",
                                     m.chat.id, st.message_id)
    try:
        size, carried = apply_ota_update(data)
    except Exception as e:
        log.warning("OTA rejected: %s", e)
        return bot.edit_message_text(f"❌ <b>Update rejected</b> — the running code was NOT changed.\n"
                                     f"<code>{esc(str(e)[:300])}</code>", m.chat.id, st.message_id)
    log_event("ota", m.from_user.id, None, f"{doc.file_name} {size}B")
    extra = f"\n🔐 Kept your existing: <code>{esc(', '.join(carried))}</code>" if carried else ""
    bot.edit_message_text(f"✅ <b>New code written</b> ({size / 1024:.1f} KB). The previous version is saved as a "
                          f"backup.{extra}\n\n♻️ Restarting now… I'll message you when I'm back "
                          f"(auto-rollback if the new code can't start within {OTA_HEALTH_WAIT_SEC}s).",
                          m.chat.id, st.message_id)
    for aid in ADMIN_CHAT_IDS:
        if aid != m.chat.id:
            safe_send(aid, f"🔄 Admin <code>{m.from_user.id}</code> pushed a code update ({size / 1024:.1f} KB). "
                           "The bot is restarting.")
    threading.Thread(target=lambda: (time.sleep(2.5), restart_self(m.chat.id)), daemon=True,
                     name="ota-restart").start()


def post_boot_tasks():
    """Runs once after every start: health flag for the OTA watchdog + 'I'm back' / 'rolled back' notices."""
    try:
        with open(OTA_OK_FLAG, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass
    try:
        if os.path.exists(OTA_RB_FLAG):
            os.remove(OTA_RB_FLAG)
            run("DELETE FROM settings WHERE key='ota_notify'")
            notify_admins("⚠️ <b>Code update rolled back</b>\nThe new script did not start correctly within "
                          f"{OTA_HEALTH_WAIT_SEC}s, so the previous version was restored automatically.")
            return
        raw = get_setting("ota_notify")
        if raw:
            run("DELETE FROM settings WHERE key='ota_notify'")
            info = json.loads(raw)
            if info.get("chat"):
                safe_send(info["chat"], "✅ <b>Bot restarted with the new code.</b>\n"
                                        f"Hosted bots recovered • uptime reset • @{esc(BOT_USERNAME)}")
    except Exception:
        log.exception("post_boot_tasks failed")


def bm_label(b):
    icon = {"running": "🟢", "stopped": "🔴", "crashed": "⚠️"}.get(b["status"], "🔴")
    tg = b["tg_username"] if b["tg_username"] not in (None, "-") else b["name"]
    owner = ("@" + b["uname"]) if b["uname"] else (b["fn"] or str(b["user_id"]))
    rt = "🐳" if (b["runtime"] or "") == "docker" else "🖥"
    return f"{icon} {rt} {tg} · {owner}"[:60]


PAGE = 8


def admin_bot_list(c, page):
    total = rows("SELECT COUNT(*) c FROM bots")[0]["c"]
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rs = rows("SELECT b.*, u.username AS uname, u.first_name AS fn FROM bots b LEFT JOIN users u ON u.user_id=b.user_id "
              "ORDER BY b.id DESC LIMIT ? OFFSET ?", (PAGE, page * PAGE))
    for b in rs:
        if b["tg_username"] is None:
            threading.Thread(target=resolve_and_store_username, args=(b["id"], b["user_id"], b["name"]),
                             daemon=True).start()
    rws = [[btn(bm_label(b), "primary", cb=f"adm:bv:{b['id']}:{page}")] for b in rs]
    nav = []
    if page > 0:
        nav.append(btn("◀️ Prev", cb=f"adm:bm:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", cb=f"adm:bm:{page}"))
    if page < pages - 1:
        nav.append(btn("Next ▶️", cb=f"adm:bm:{page + 1}"))
    rws.append(nav)
    rws.append([btn("⬅️ Admin menu", cb="adm:menu")])
    running = sum(1 for x in list(procs.values()) if x["thread"].is_alive())
    adm_edit(c, f"🤖 <b>Ultimate Bot Manager</b>\n{DIV}\n{total} bots hosted • {running} running\n"
                "Tap a bot to control it 👇", inline(rws))


def admin_bot_view(c, bid, page):
    rs = rows("SELECT b.*, u.username AS uname, u.first_name AS fn FROM bots b LEFT JOIN users u ON u.user_id=b.user_id "
              "WHERE b.id=?", (bid,))
    if not rs:
        return adm_edit(c, "Bot no longer exists.", inline([[btn("⬅️ Back", cb=f"adm:bm:{page}")]]))
    b = rs[0]
    st = {"running": "🟢 Running", "stopped": "🔴 Stopped", "crashed": "⚠️ Crashed"}.get(b["status"], b["status"])
    ram = load_manager.rss.get(bid)
    tg = b["tg_username"] if b["tg_username"] not in (None, "-") else "unknown / resolving…"
    rt = b["runtime"] or "subprocess"
    rt_label = "🐳 Docker" if rt == "docker" else "🖥 Subprocess"
    if is_admin(b["user_id"]):
        rt_label += " 👑 God Mode"
    text = (f"🤖 <b>{esc(b['name'])}</b>\n{DIV}\n🔗 Bot: {esc(tg)}\n"
            f"👤 Owner: <code>{b['user_id']}</code> {esc('@' + b['uname']) if b['uname'] else esc(b['fn'])}\n"
            f"Status: {st}\nRuntime: {rt_label}\n⏱ Uptime: {bot_uptime(bid)}"
            + (f" • 🧠 {ram:.0f} MB" if ram else "") +
            f"\nAuto-restart: {'ON' if b['autorestart'] else 'OFF'}\nCreated: {esc((b['created'] or '')[:16])} UTC")
    ctl = [btn("⏹ Stop", "danger", cb=f"adm:ba:stop:{bid}:{page}")] if b["status"] == "running" \
        else [btn("▶️ Start", "success", cb=f"adm:ba:start:{bid}:{page}")]
    ctl.append(btn("🔄 Restart", "primary", cb=f"adm:ba:restart:{bid}:{page}"))
    adm_edit(c, text, inline([
        ctl,
        [btn("📜 Logs", "primary", cb=f"adm:ba:logs:{bid}:{page}"), btn("📥 Source", "primary", cb=f"adm:ba:file:{bid}:{page}")],
        [btn("🐳 Docker logs", "primary", cb=f"adm:ba:dlogs:{bid}:{page}")],
        [btn("🚫 Ban owner", "danger", cb=f"adm:ba:banown:{bid}:{page}"), btn("🗑 Delete", "danger", cb=f"adm:ba:delq:{bid}:{page}")],
        [btn("⬅️ Back to list", cb=f"adm:bm:{page}")],
    ]))


def server_health_text():
    s = system_stats()
    with procs_lock:
        live = sum(1 for x in procs.values() if x["thread"].is_alive())
        docker_running = sum(1 for x in procs.values() if x.get("mode") == "docker")
    top = sorted(load_manager.rss.items(), key=lambda kv: -kv[1])[:5]
    top_lines = []
    for bid, mb in top:
        b = get_bot(bid)
        top_lines.append(f"• {esc(b['name'] if b else bid)} — {mb:.0f} MB, CPU {load_manager.cpu.get(bid, 0):.0f}%")
    return (f"🖥 <b>Server Health</b>\n{DIV}\n"
            f"🧠 RAM  {pbar(s['ram_pct'])} {s['ram_pct']:.0f}% ({s['ram_used']:.0f}/{s['ram_total']:.0f} MB)\n"
            f"⚙️ CPU  {pbar(s['cpu'])} {s['cpu']:.0f}%\n"
            f"💽 Disk {pbar(s['disk_pct'])} {s['disk_pct']:.0f}%\n"
            f"📈 Load avg: {s['load'][0]:.2f} / {s['load'][1]:.2f} / {s['load'][2]:.2f} ({s['cores']} cores)\n"
            f"⏱ Uptime: {fmt_dur(time.time() - START_TIME)}\n"
            f"🤖 Live bot processes: {live} ({docker_running} in Docker) • 📥 Upload queue: {upload_q.qsize()}\n"
            f"👑 Admins: {len(ADMIN_CHAT_IDS)}\n"
            f"{DIV}\n🔝 <b>Top bots by RAM</b>\n" + ("\n".join(top_lines) or "—") +
            f"\n{DIV}\n🧩 Fixer: {'✅ autopep8' if autopep8 else '⚠️ local only'} • 🛡 Load Mgr: "
            f"{'✅ psutil' if psutil else '⚠️ /proc'} • 🤖 AI: {'✅ g4f' if (G4FClient and AI_ENABLED) else '⚠️ rules only'} • "
            f"{docker_mgr.status_text()}")


def dashboard_text():
    total = rows("SELECT COUNT(*) c FROM users")[0]["c"]
    prem = rows("SELECT COUNT(*) c FROM users WHERE premium_until > ?", (utcnow().isoformat(),))[0]["c"]
    banned = rows("SELECT COUNT(*) c FROM users WHERE banned=1")[0]["c"]
    hosted = rows("SELECT COUNT(*) c FROM bots")[0]["c"]
    crashed = rows("SELECT COUNT(*) c FROM bots WHERE status='crashed'")[0]["c"]
    paid = rows("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM orders WHERE status='paid'")[0]
    with procs_lock:
        live = sum(1 for x in procs.values() if x["thread"].is_alive())
        docker_count = sum(1 for x in procs.values() if x.get("mode") == "docker")
    active = rows("SELECT COUNT(DISTINCT user_id) c FROM bots WHERE status='running'")[0]["c"]
    return (f"📊 <b>Dashboard</b>\n{DIV}\n👥 Users: {total}\n💎 Premium: {prem}\n🚫 Banned: {banned}\n"
            f"🤖 Hosted bots: {hosted}\n🟢 Running now: {live} (🐳 {docker_count} in Docker)\n⚠️ Crashed: {crashed}\n"
            f"🔥 Users with running bots: {active}\n💰 Paid orders: {paid['c']} (₹{paid['s']:.0f} total)\n"
            f"{DIV}\n{docker_mgr.status_text()}")


def _users_page(c, page):
    total = rows("SELECT COUNT(*) c FROM users")[0]["c"]
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rs = rows("SELECT * FROM users ORDER BY user_id LIMIT ? OFFSET ?", (PAGE, page * PAGE))
    rws = []
    for u in rs:
        icon = "👑" if is_admin(u["user_id"]) else ("💎" if is_premium(u["user_id"]) else ("🚫" if u["banned"] else "👤"))
        rws.append([btn(f"{icon} {u['user_id']} · {(u['first_name'] or '?')[:18]}", "primary", cb=f"adm:uv:{u['user_id']}:{page}")])
    nav = []
    if page > 0:
        nav.append(btn("◀️ Prev", cb=f"adm:ulist:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", cb=f"adm:ulist:{page}"))
    if page < pages - 1:
        nav.append(btn("Next ▶️", cb=f"adm:ulist:{page + 1}"))
    rws.append(nav)
    rws.append([btn("⬅️ User Manager", cb="adm:um")])
    adm_edit(c, f"👥 <b>Users</b>\n{DIV}\n{total} total", inline(rws))


def _user_view(c, uid, page):
    u = get_user(uid)
    if not u:
        return adm_edit(c, "User not found.", inline([[btn("⬅️ Back", cb=f"adm:ulist:{page}")]]))
    bl = rows("SELECT * FROM bots WHERE user_id=? ORDER BY id", (uid,))
    bot_lines = "\n".join(f"• {b['name']} ({b['status']}, {b['runtime'] or 'subprocess'})" for b in bl) or "—"
    plan = "👑 Admin" if is_admin(uid) else ("💎 " + (u["premium_until"] or "")[:10] if is_premium(uid) else "🆓 Free")
    adm_edit(c,
             f"👤 <b>User {uid}</b>\n{DIV}\n"
             f"Name: {esc(u['first_name'])} (@{esc(u['username'])})\n"
             f"Plan: {esc(plan)}\n"
             f"Free used: {u['lifetime_free_used'] or 0} • Bonus slots: {u['extra_slots']}\n"
             f"Banned: {'🚫 Yes' if u['banned'] else '✅ No'}\n"
             f"Joined: {esc((u['joined_at'] or '')[:10])}\n\n🤖 Bots ({len(bl)}):\n{esc(bot_lines)}",
             inline([
                 [btn("➕ Grant 30 days", "success", cb=f"adm:ugp:{uid}:30"),
                  btn("➖ Remove Premium", "danger", cb=f"adm:urp:{uid}")],
                 [btn("🚫 Ban", "danger", cb=f"adm:ubn:{uid}"), btn("✅ Unban", "success", cb=f"adm:uub:{uid}")],
                 [btn("⬅️ Back", cb=f"adm:ulist:{page}")],
             ]))


def admin_redeem_text():
    total = rows("SELECT COUNT(*) c FROM redeem_codes")[0]["c"]
    used = rows("SELECT COUNT(*) c FROM redeem_codes WHERE used_by IS NOT NULL")[0]["c"]
    return (f"🎟️ <b>Redeem Code Manager</b>\n{DIV}\n"
            f"Total codes: <b>{total}</b> • Redeemed: <b>{used}</b>\n\n"
            "Generate single-use codes for channel giveaways. Each code grants the selected number of hosting credits.")


def admin_redeem_menu():
    return inline([[btn("➕ Generate Codes", "success", cb="adm:redeemnew")],
                   [btn("👥 Redeemed Users / Code List", "primary", cb="adm:redeemlist")],
                   [btn("⬅️ Admin menu", cb="adm:menu")]])


@safe
def step_redeem_credits(m):
    if cancelled(m): return
    t = (m.text or "").strip()
    if not t.isdigit() or not 1 <= int(t) <= 1000:
        return bot.send_message(m.chat.id, "❌ Send a number from 1 to 1000 for hosting credits.", reply_markup=admin_main_menu())
    msg = bot.send_message(m.chat.id, "How many unique redeem codes should be generated? Send a number from 1 to 100.")
    bot.register_next_step_handler(msg, lambda nxt: step_redeem_quantity(nxt, int(t)))


@safe
def step_redeem_quantity(m, credits):
    if cancelled(m): return
    t = (m.text or "").strip()
    if not t.isdigit() or not 1 <= int(t) <= 100:
        return bot.send_message(m.chat.id, "❌ Send a number from 1 to 100.", reply_markup=admin_main_menu())
    quantity = int(t)
    made = []
    for _ in range(quantity):
        for attempt in range(5):
            code = "LX-" + secrets.token_hex(5).upper()
            try:
                run("INSERT INTO redeem_codes(code,credits,created_by,created_at) VALUES(?,?,?,?)",
                    (code, credits, m.from_user.id, utcnow().isoformat(timespec="seconds")))
                made.append(code)
                break
            except sqlite3.IntegrityError:
                continue
    if not made:
        return bot.send_message(m.chat.id, "❌ No codes could be generated. Please try again.", reply_markup=admin_main_menu())
    log_event("redeem_create", uid=m.from_user.id, detail=f"count={len(made)} credits={credits}")
    payload = (f"✅ <b>Redeem Codes Generated</b>\nCredits per code: <b>{credits}</b>\n"
               f"Codes created: <b>{len(made)}</b>\nEach code is single-use.\n\n" +
               "\n".join(f"<code>{esc(code)}</code>" for code in made))
    bot.send_message(m.chat.id, payload, reply_markup=admin_redeem_menu())


def admin_redeem_list(chat_id):
    items = rows("SELECT code,credits,used_by,used_username,created_at,redeemed_at FROM redeem_codes ORDER BY id DESC LIMIT 40")
    if not items:
        text = "🎟️ <b>Redeemed Users / Code List</b>\nNo redeem codes have been created yet."
    else:
        lines = []
        for r in items:
            if r["used_by"] is not None:
                username = ("@" + r["used_username"]) if r["used_username"] else "No username"
                status = f"✅ Used by {esc(username)} (<code>{r['used_by']}</code>)"
            else:
                status = "🟡 Unused"
            lines.append(f"<code>{esc(r['code'])}</code> — {r['credits']} credit(s)\n{status}")
        text = "🎟️ <b>Redeem Codes / Redeemed Users</b>\n" + DIV + "\n\n" + "\n\n".join(lines)
    bot.send_message(chat_id, text, reply_markup=inline([[btn("🔄 Refresh", "primary", cb="adm:redeemlist")],
                                                        [btn("⬅️ Redeem Manager", cb="adm:redeem")]]))


def admin_cb(c):
    if not is_admin(c.from_user.id):
        return bot.answer_callback_query(c.id, "Not allowed.", show_alert=True)
    chat = c.message.chat.id
    parts = c.data.split(":")
    act = parts[1]
    bot.answer_callback_query(c.id)
    back = inline([[btn("⬅️ Admin menu", cb="adm:menu")]])
    if act == "redeem":
        return adm_edit(c, admin_redeem_text(), admin_redeem_menu())
    if act == "redeemnew":
        return ask(chat, "🎟️ <b>Generate Redeem Codes</b>\nHow many hosting credits should each code grant? (1–1000)", step_redeem_credits)
    if act == "redeemlist":
        # Send a separate full list to avoid Telegram's edit-message length limit.
        return admin_redeem_list(chat)
    if act == "menu":
        return adm_edit(c, f"🛠 <b>Admin Panel</b>\n{DIV}\n\nFull control over users, bots, Docker and the server 👇",
                        admin_main_menu())
    if act == "stats":
        return adm_edit(c, dashboard_text(), inline([[btn("🔄 Refresh", "primary", cb="adm:stats"),
                                                      btn("⬅️ Menu", cb="adm:menu")]]))
    if act == "srv":
        return adm_edit(c, "🖥 <b>Server Control</b>\nChoose an action 👇", admin_srv_menu())
    if act == "srhealth":
        return adm_edit(c, server_health_text(), inline([[btn("🔄 Refresh", "primary", cb="adm:srhealth"),
                                                          btn("⬅️ Server", cb="adm:srv")]]))
    if act == "top":
        top = sorted(load_manager.rss.items(), key=lambda kv: -kv[1])[:10]
        lines = []
        for bid, mb in top:
            b = get_bot(bid)
            lines.append(f"• <b>{esc(b['name'] if b else bid)}</b> — 🧠 {mb:.0f} MB • CPU {load_manager.cpu.get(bid, 0):.0f}%")
        return adm_edit(c, "📈 <b>Top Bots by RAM</b>\n" + ("\n".join(lines) or "—"),
                        inline([[btn("⬅️ Server", cb="adm:srv")]]))
    if act == "dk":
        cs = docker_mgr.list_containers()
        lines = []
        for x in cs[:20]:
            try:
                lines.append(f"• <code>{esc(x.name)}</code> — {esc(x.status)}")
            except Exception:
                lines.append("• (unreadable)")
        return adm_edit(c, f"🐳 <b>Docker Panel</b>\n{DIV}\n{docker_mgr.status_text()}\n\n"
                           f"Containers ({len(cs)}):\n" + ("\n".join(lines) or "—"),
                        inline([[btn("🔄 Reconnect Docker", "primary", cb="adm:dkrecon"),
                                 btn("⬅️ Menu", cb="adm:menu")]]))
    if act == "dkrecon":
        ok = docker_mgr.connect()
        return adm_edit(c, f"🐳 Docker reconnect: {'✅ ' + docker_mgr.status_text() if ok else '❌ ' + docker_mgr.reason}",
                        inline([[btn("⬅️ Docker", cb="adm:dk"), btn("⬅️ Menu", cb="adm:menu")]]))
    if act == "adv":
        adm_edit(c, "🧠 <b>Advisor</b> is analysing the last 24 hours…", None)

        def work():
            txt = agent5_summary()
            try:
                bot.edit_message_text(txt, chat, c.message.message_id,
                                      reply_markup=inline([[btn("🔄 Re-analyse", "primary", cb="adm:adv"),
                                                            btn("⬅️ Menu", cb="adm:menu")]]))
            except Exception:
                safe_send(chat, txt, reply_markup=back)
        return threading.Thread(target=work, daemon=True).start()
    if act == "evt":
        ev = rows("SELECT * FROM events ORDER BY id DESC LIMIT 20")
        lines = [f"<code>{esc(e['ts'][5:16])}</code> <b>{esc(e['kind'])}</b> u={e['user_id'] or '-'} "
                 f"{esc((e['detail'] or '')[:60])}" for e in ev]
        return adm_edit(c, f"📋 <b>Activity Log</b>\n{DIV}\n" + ("\n".join(lines) or "No events yet."), back)
    if act == "bm":
        return admin_bot_list(c, int(parts[2]))
    if act == "bv":
        return admin_bot_view(c, int(parts[2]), int(parts[3]))
    if act == "ba":
        sub, bid, page = parts[2], int(parts[3]), int(parts[4])
        b = get_bot(bid)
        if not b:
            return adm_edit(c, "Bot no longer exists.", inline([[btn("⬅️ Back", cb=f"adm:bm:{page}")]]))
        if sub == "start":
            start_bot(bid)
        elif sub == "stop":
            stop_bot(bid)
        elif sub == "restart":
            stop_bot(bid)
            start_bot(bid)
        elif sub == "logs":
            t = tail(log_path(bid)).strip() or "(no output yet)"
            msg = bot.send_message(chat, f"📜 <b>Logs — {esc(b['name'])}</b>\n<pre>{esc(t[-3500:])}</pre>")
            return auto_delete(chat, msg.message_id, delay=60)
        elif sub == "dlogs":
            t = docker_mgr.logs(bid, b["name"], tail=200).strip() or "(no docker logs)"
            return bot.send_message(chat, f"🐳 <b>Docker logs — {esc(b['name'])}</b>\n<pre>{esc(t[-3500:])}</pre>")
        elif sub == "file":
            path = os.path.join(bot_dir(b["user_id"], b["name"]), b["name"] + ".py")
            try:
                with open(path, "rb") as f:
                    return bot.send_document(chat, (b["name"] + ".py", f.read()),
                                             caption=f"Source of {esc(b['name'])} (user {b['user_id']})")
            except Exception as e:
                return bot.send_message(chat, f"❌ Could not read file: {esc(e)}")
        elif sub == "banown":
            if is_admin(b["user_id"]):
                return bot.send_message(chat, "❌ You can't ban an admin.")
            ban_user(b["user_id"])
            log_event("ban", b["user_id"], bid, "via bot manager")
            safe_send(b["user_id"], f"🚫 Your account has been suspended. Contact @{DEVELOPER_USERNAME}.")
        elif sub == "delq":
            return adm_edit(c, f"🗑 Delete <b>{esc(b['name'])}</b> (user <code>{b['user_id']}</code>) and all its files?",
                            inline([[btn("✅ Yes, delete", "danger", cb=f"adm:ba:del:{bid}:{page}"),
                                     btn("❌ Cancel", cb=f"adm:bv:{bid}:{page}")]]))
        elif sub == "del":
            delete_bot(b)
            log_event("delete", b["user_id"], bid, "by admin")
            return admin_bot_list(c, page)
        time.sleep(0.4)
        return admin_bot_view(c, bid, page)
    # --- User Manager ---
    if act == "um":
        return adm_edit(c, "👥 <b>User Manager</b>\nChoose an action 👇", admin_um_menu())
    if act == "ulist":
        return _users_page(c, int(parts[2]))
    if act == "uv":
        return _user_view(c, int(parts[2]), int(parts[3]))
    if act == "ugp":
        uid, days = int(parts[2]), int(parts[3])
        until = activate_premium(uid, days)
        try:
            safe_send(uid, f"🎉 <b>Premium activated!</b> Valid until <b>{until:%Y-%m-%d}</b>.")
        except Exception:
            pass
        return adm_edit(c, f"✅ Granted {days}d to <code>{uid}</code> until {until:%Y-%m-%d}.",
                        inline([[btn("⬅️ Back", cb="adm:um")]]))
    if act == "urp":
        uid = int(parts[2])
        run("UPDATE users SET premium_until=NULL WHERE user_id=?", (uid,))
        return adm_edit(c, f"✅ Removed premium from <code>{uid}</code>.",
                        inline([[btn("⬅️ Back", cb="adm:um")]]))
    if act == "ubn":
        uid = int(parts[2])
        if is_admin(uid):
            return adm_edit(c, "❌ Can't ban an admin.", inline([[btn("⬅️ Back", cb="adm:um")]]))
        ban_user(uid)
        return adm_edit(c, f"🚫 Banned <code>{uid}</code>.", inline([[btn("⬅️ Back", cb="adm:um")]]))
    if act == "uub":
        uid = int(parts[2])
        run("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
        return adm_edit(c, f"✅ Unbanned <code>{uid}</code>.", inline([[btn("⬅️ Back", cb="adm:um")]]))
    if act == "usr":
        return ask(chat, "Send the user ID to look up:", step_user_lookup)
    if act == "addp":
        return ask(chat, "Send: <code>user_id days</code> (e.g. <code>123456789 30</code>)", step_addp)
    if act == "remp":
        return ask(chat, "Send the user ID to remove premium from:", step_remp)
    if act == "bonus":
        return ask(chat, "Send: <code>user_id amount</code> (e.g. <code>123456789 3</code> or <code>123456789 -1</code>)", step_bonus)
    if act == "ban":
        return ask(chat, "Send the user ID to ban (stops all their bots):", step_ban)
    if act == "unban":
        return ask(chat, "Send the user ID to unban:", step_unban)
    if act == "bc":
        return ask(chat, "Send the message to broadcast (text, photo, anything):", step_broadcast)
    # --- Security Panel ---
    if act == "sec":
        return adm_edit(c, "🛡 <b>Security Panel</b>\nChoose an action 👇", admin_sec_menu())
    if act == "secrej":
        ev = rows("SELECT * FROM events WHERE kind='reject' ORDER BY id DESC LIMIT 20")
        lines = [f"<code>{esc(e['ts'][5:16])}</code> u={e['user_id'] or '-'} {esc((e['detail'] or '')[:80])}"
                 for e in ev]
        return adm_edit(c, "📋 <b>Recent Rejections</b>\n" + ("\n".join(lines) or "None."),
                        inline([[btn("⬅️ Security", cb="adm:sec")]]))
    if act == "seckil":
        ev = rows("SELECT * FROM events WHERE kind='kill' ORDER BY id DESC LIMIT 20")
        lines = [f"<code>{esc(e['ts'][5:16])}</code> bot={e['bot_id'] or '-'} {esc((e['detail'] or '')[:80])}"
                 for e in ev]
        return adm_edit(c, "🛡 <b>Recent Kills</b>\n" + ("\n".join(lines) or "None."),
                        inline([[btn("⬅️ Security", cb="adm:sec")]]))
    if act == "secinfo":
        txt = ("🛡 <b>Security Scanner</b>\n" + DIV + "\n\n"
               "<b>Blocked patterns (plain text):</b>\n"
               "• SMS / call bomber code, bulk SMS gateways\n"
               "• Nested hosting-bot platforms\n"
               "• Phone/contact scrapers, getChatMembers dumps\n"
               "• Mass request flood loops\n"
               "• Thread pools with 300+ workers\n"
               "• Server directory sweeps (/, /root, /home)\n"
               "• Reads of /etc/passwd, /proc environ, SSH keys\n"
               "• Token-searching regex + walker combinations\n"
               "• eval()/exec()/compile()/__import__() calls\n"
               "• File deletion (os.remove, shutil.rmtree …)\n"
               "• Reverse-shell patterns (socket + process redirection)\n"
               "• Destructive shell commands (rm -rf, mkfs, dd, curl|sh)\n"
               "• References to the hosting platform's own files")
        return adm_edit(c, txt, inline([[btn("⬅️ Security", cb="adm:sec")]]))
    # --- Settings / Maintenance / Payments ---
    if act == "set":
        return adm_edit(c, "⚙️ <b>Settings</b>\nChoose an action 👇", admin_set_menu())
    if act == "prices":
        pl = rows("SELECT * FROM plans ORDER BY days")
        return bot.send_message(chat, "💰 <b>Select a plan to change its price</b>", reply_markup=inline(
            [[btn(f"{p['label']} — ₹{p['price']}", "primary", cb=f"adm:pp:{p['id']}")] for p in pl]))
    if act == "pp":
        pid = int(parts[2])
        return ask(chat, "Send the new price in INR (number only):", lambda m: step_price(m, pid))
    if act == "maint":
        return adm_edit(c, "🔧 <b>Maintenance</b>\nChoose an action 👇", admin_maint_menu())
    if act == "recover":
        return adm_edit(c, "🧬 Recovering orphaned bots…",
                        inline([[btn("⬅️ Maintenance", cb="adm:maint")]]))
    if act == "pay":
        return adm_edit(c, "💰 <b>Payments</b>\nChoose a filter 👇", admin_pay_menu())
    if act == "paylist":
        status, page = parts[2], int(parts[3])
        total = rows("SELECT COUNT(*) c FROM orders WHERE status=?", (status,))[0]["c"]
        pages = max(1, (total + 10 - 1) // 10)
        page = max(0, min(page, pages - 1))
        rs = rows("SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT 10 OFFSET ?", (status, page * 10))
        lines = [f"<code>{esc(o['order_id'])}</code> — <code>{o['user_id']}</code> — ₹{o['amount']:.0f} — "
                 f"{esc((o['created'] or '')[:16])}" for o in rs]
        nav = []
        if page > 0:
            nav.append(btn("◀️ Prev", cb=f"adm:paylist:{status}:{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", cb=f"adm:paylist:{status}:{page}"))
        if page < pages - 1:
            nav.append(btn("Next ▶️", cb=f"adm:paylist:{status}:{page + 1}"))
        rws = []
        if nav:
            rws.append(nav)
        rws.append([btn("⬅️ Payments", cb="adm:pay")])
        return adm_edit(c, f"💰 <b>Orders — {esc(status)}</b>\n" + ("\n".join(lines) or "None."), inline(rws))
    # --- OTA code update / storage clean ---
    if act == "ota":
        return ask(chat, "🔄 <b>Update Bot Code</b>\n" + DIV + "\n\nSend the new <code>.py</code> script now (as a "
                         "<b>document</b>).\nI will validate it, back up the current version, overwrite myself and "
                         "restart. If the new code can't start, the old version is restored automatically.\n"
                         "An empty <code>BOT_TOKEN = \"\"</code> in the new file keeps your current token.",
                   step_ota_update)
    if act == "clean":
        freed, removed = sweep_orphans(min_age=60)
        lines = "\n".join("• " + esc(x) for x in removed[:25]) or "Nothing to clean — server is already tidy ✅"
        return adm_edit(c, f"🧹 <b>Deep Clean</b>\n{DIV}\nFreed: <b>{freed / 2**20:.1f} MB</b> • removed {len(removed)} item(s)\n\n"
                           + lines, inline([[btn("⬅️ Maintenance", cb="adm:maint")]]))
    # --- Welcome media ---
    if act == "wm":
        has = bool(get_setting("welcome_media"))
        return adm_edit(c, f"🎬 <b>Welcome Media</b>\n{DIV}\nCurrent: {'✅ set' if has else '❌ none'}\n"
                           "The video/GIF is attached to the /start message new users see (use 16:9 for a YouTube look).",
                        inline([[btn("⬆️ Upload new", "success", cb="adm:wmu")],
                                [btn("👁 Preview", "primary", cb="adm:wmp"), btn("🗑 Remove", "danger", cb="adm:wmd")],
                                [btn("⬅️ Admin menu", cb="adm:menu")]]))
    if act == "wmu":
        return ask(chat, "Send the welcome <b>video</b> or <b>GIF</b> now:", step_welcome)
    if act == "wmp":
        return send_welcome(chat, "👋 <b>Preview of the welcome message</b>", None)
    if act == "wmd":
        run("DELETE FROM settings WHERE key='welcome_media'")
        return bot.send_message(chat, "🗑 Welcome media removed.", reply_markup=admin_main_menu())
    # --- Maintenance actions ---
    if act == "kf":
        acts = load_manager.check(force=True)
        return bot.send_message(chat, "🧹 <b>Load Manager pass done.</b>\n" +
                                ("\n".join("• " + esc(a) for a in acts) if acts else "No frozen or oversized bots found ✅"),
                                reply_markup=admin_main_menu())
    if act == "bk":
        def a():
            tmp = os.path.join(tempfile.gettempdir(), f"lx_backup_{int(time.time())}.db")
            src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(tmp)
            src.backup(dst)
            src.close()
            dst.close()
            return tmp

        def b():
            tmp = os.path.join(tempfile.gettempdir(), f"lx_backup_{int(time.time())}.db")
            shutil.copyfile(DB_PATH, tmp)
            return tmp
        tmp, tier = triple("db_backup", a, b, None)
        if not tmp:
            return bot.send_message(chat, "❌ Backup failed (see logs).", reply_markup=admin_main_menu())
        try:
            with open(tmp, "rb") as f:
                bot.send_document(chat, (os.path.basename(tmp), f.read()), caption=f"💾 DB backup (plan {tier})")
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return
    if act in ("ra", "sa"):
        word = "restart" if act == "ra" else "stop"
        return adm_edit(c, f"⚠️ <b>{word.title()} ALL hosted bots?</b>\nThis affects every user.",
                        inline([[btn(f"✅ Yes, {word} all", "danger", cb=f"adm:{act}c"), btn("❌ Cancel", cb="adm:menu")]]))
    if act in ("rac", "sac"):
        def work():
            ids = [r["id"] for r in rows("SELECT id FROM bots")]
            for i in ids:
                try:
                    stop_bot(i)
                    if act == "rac":
                        start_bot(i)
                        time.sleep(0.3)
                except Exception:
                    log.exception("bulk action failed for bot %s", i)
            safe_send(chat, f"✅ Bulk action finished for {len(ids)} bots.", reply_markup=admin_main_menu())
        adm_edit(c, "⏳ Working on all bots…", None)
        return threading.Thread(target=work, daemon=True).start()


# ----------------------------- fallback handler ------------------------
@bot.message_handler(func=lambda m: True, content_types=["text"])
@safe
def fallback(m):
    uid = m.from_user.id
    if blocked(uid) is not None:
        return
    text = m.text or ""
    bot.send_message(m.chat.id, "Use the menu below, or send a <code>.py</code> file to host a bot.",
                     reply_markup=main_menu(m.chat.id))


# -------------------------- Flask webhook + status API --------------------------
def initialize_runtime():
    """Initialize this process once. Webhook requests are the only Telegram update source; no polling."""
    global BOT_USERNAME, _runtime_initialized
    if _runtime_initialized:
        return
    with _runtime_init_lock:
        if _runtime_initialized:
            return
        if not BOT_TOKEN:
            raise RuntimeError("Set BOT_TOKEN in the CONFIGURATION section before starting the bot")
        init_db()
        try:
            backfill_token_hashes()
            freed, removed = sweep_orphans(min_age=0)
            if removed:
                log.info("Startup sweep removed %d orphan item(s), freed %.1f MB", len(removed), freed / 2**20)
        except Exception:
            log.exception("startup cleanup failed (continuing)")
        try:
            BOT_USERNAME = bot.get_me().username
        except Exception:
            log.exception("Telegram getMe failed during initialization")
            raise
        # Existing supervisors/workers are retained for persistent Flask hosts. Serverless
        # platforms may freeze/terminate these threads after a request; see deployment caveat.
        for i in range(UPLOAD_WORKERS):
            keep_alive(upload_worker, f"upload-worker-{i}")
        if not load_manager.is_alive():
            load_manager.start()
        keep_alive(maintenance_loop, "maintenance")
        threading.Thread(target=recover_bots, daemon=True).start()
        threading.Thread(target=backfill_usernames, daemon=True).start()
        threading.Thread(target=post_boot_tasks, daemon=True).start()
        if PUBLIC_BASE_URL:
            try:
                bot.set_webhook(url=PUBLIC_BASE_URL + WEBHOOK_PATH,
                                secret_token=WEBHOOK_SECRET,
                                drop_pending_updates=False,
                                max_connections=20)
                log.info("Telegram webhook configured at %s%s", PUBLIC_BASE_URL, WEBHOOK_PATH)
            except Exception:
                log.exception("Could not configure Telegram webhook; configure it through Telegram Bot API")
        _runtime_initialized = True
        log.info("LX Hosting PRO v3 webhook app initialized as @%s", BOT_USERNAME)


@app.before_request
def _ensure_initialized():
    # Catalyst/WSGI imports the module rather than running it as a script.
    initialize_runtime()


@app.route("/", methods=["GET"])
def home():
    return Response("LX Hosting PRO webhook service is online.", mimetype="text/plain")


@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secrets.compare_digest(supplied, WEBHOOK_SECRET):
        return jsonify({"ok": False, "error": "unauthorized webhook"}), 403
    try:
        payload = request.get_json(silent=False)
        update = types.Update.de_json(payload)
        bot.process_new_updates([update])
        return jsonify({"ok": True})
    except Exception:
        log.exception("Telegram webhook update processing failed")
        # Return 500 so Telegram can retry transient handler failures.
        return jsonify({"ok": False, "error": "update processing failed"}), 500


def _public_bot_status(public_id):
    found = rows("SELECT id,user_id,name,status,created,runtime,public_id FROM bots WHERE public_id=?", (public_id,))
    if not found:
        return None
    b = found[0]
    ctl = procs.get(b["id"])
    live = bool(ctl and ctl.get("thread") and ctl["thread"].is_alive()
                and ctl.get("proc") is not None and ctl["proc"].poll() is None) if ctl and b["runtime"] != "docker" else bool(ctl and ctl.get("thread") and ctl["thread"].is_alive())
    # Process state is more authoritative when the supervisor exists; DB status is the fallback.
    active = live if ctl else b["status"] == "running"
    cpu = load_manager.cpu.get(b["id"]) if "load_manager" in globals() else None
    ram = load_manager.rss.get(b["id"]) if "load_manager" in globals() else None
    system = system_stats()
    return {
        "bot_id": b["public_id"], "name": b["name"],
        "status": "running" if active else (b["status"] if b["status"] in ("stopped", "crashed") else "stopped"),
        "active": bool(active), "runtime": b["runtime"] or "subprocess",
        "uptime": bot_uptime(b["id"]), "cpu_percent": round(float(cpu), 2) if cpu is not None else None,
        "ram_mb": round(float(ram), 2) if ram is not None else None,
        "server_cpu_percent": round(float(system.get("cpu", 0)), 2),
        "server_ram_percent": round(float(system.get("ram_pct", 0)), 2),
        "checked_at_utc": datetime.now(timezone.utc).isoformat()
    }


@app.route("/status/<string:bot_id>", methods=["GET"])
def bot_status_endpoint(bot_id):
    data = _public_bot_status(bot_id)
    if data is None:
        return jsonify({"error": "bot not found"}), 404
    if request.args.get("format", "html").lower() == "json" or "application/json" in request.headers.get("Accept", ""):
        return jsonify(data)
    status_color = "#16803c" if data["active"] else "#b42318"
    def metric(value, suffix=""):
        return "Unavailable" if value is None else f"{value}{suffix}"
    html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="15"><title>Bot status</title>
<style>body{{font-family:system-ui,sans-serif;background:#f4f6f8;color:#17202a;margin:0;padding:24px}}main{{max-width:640px;margin:5vh auto;background:white;padding:24px;border-radius:16px;box-shadow:0 8px 30px #0001}}h1{{margin-top:0}}.status{{color:{status_color};font-weight:700}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.item{{background:#f4f6f8;padding:14px;border-radius:10px}}small{{color:#667085}}code{{overflow-wrap:anywhere}}</style></head>
<body><main><h1>🤖 {escape(str(data['name']))}</h1><p class="status">● {escape(data['status'].upper())}</p>
<p><small>Bot ID</small><br><code>{escape(data['bot_id'])}</code></p><div class="grid">
<div class="item"><small>Bot CPU</small><br><b>{escape(metric(data['cpu_percent'], '%'))}</b></div>
<div class="item"><small>Bot RAM</small><br><b>{escape(metric(data['ram_mb'], ' MB'))}</b></div>
<div class="item"><small>Uptime</small><br><b>{escape(data['uptime'])}</b></div>
<div class="item"><small>Runtime</small><br><b>{escape(data['runtime'])}</b></div>
<div class="item"><small>Server CPU</small><br><b>{escape(metric(data['server_cpu_percent'], '%'))}</b></div>
<div class="item"><small>Server RAM</small><br><b>{escape(metric(data['server_ram_percent'], '%'))}</b></div>
</div><p><small>Last checked: {escape(data['checked_at_utc'])}. Refreshes every 15 seconds.</small></p>
<p><a href="?format=json">View JSON status</a></p></main></body></html>'''
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"ok": True, "service": "lx-hosting-pro", "webhook": "enabled", "polling": False})


def main():
    """Local persistent-host entry point. Zoho Catalyst should import `app` as its WSGI app."""
    initialize_runtime()
    # No bot.polling()/infinity_polling(): Telegram updates arrive only through the webhook route.
    port = 8080
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
