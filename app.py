import os
import re
import json
import time
import base64
import sqlite3
import requests

from flask import Flask, request, jsonify
from tonsdk.contract.wallet import Wallets, WalletVersionEnum
from tonsdk.utils import to_nano

# =========================================================
# SOZLAMALAR
# =========================================================

BOT_TOKEN = "7749042035:AAGbbDPSZX5R3aOnbILO4iJu4INAeXlnCSA"
ADMIN_ID = 8407175251

DEFAULT_CARD_NUMBER = "9860 1606 3706 4553"
DEFAULT_TON_RATE = 17000  # 1 TON narxi so'mda

TONCENTER_API_KEY = "d422b90c884673b8756debf4c9fab3682a4835884436a923981e0b82f215dbfc"
TONCENTER_BASE_URL = "https://toncenter.com/api/v2"

# fallback mnemonic (agar db da mnemonic bo'lmasa ishlatiladi)
MNEMONICS = """
word1 word2 word3 word4 word5 word6 word7 word8
word9 word10 word11 word12 word13 word14 word15 word16
word17 word18 word19 word20 word21 word22 word23 word24
""".strip()

PORT = int(os.environ.get("PORT", 10000))
DB_PATH = "bot.db"

BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)

# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        step TEXT,
        temp_wallet TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        wallet_address TEXT NOT NULL,
        amount_ton REAL NOT NULL,
        amount_uzs INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'waiting_payment',
        tx_data TEXT,
        error_text TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    cur.execute("SELECT value FROM settings WHERE key='ton_rate'")
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("ton_rate", str(DEFAULT_TON_RATE))
        )

    cur.execute("SELECT value FROM settings WHERE key='card_number'")
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("card_number", DEFAULT_CARD_NUMBER)
        )

    cur.execute("SELECT value FROM settings WHERE key='mnemonic'")
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("mnemonic", MNEMONICS)
        )

    conn.commit()
    conn.close()


init_db()

# =========================================================
# SETTINGS
# =========================================================

def get_ton_rate():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key='ton_rate'")
    row = cur.fetchone()
    conn.close()
    if not row:
        return DEFAULT_TON_RATE
    return int(row["value"])


def set_ton_rate(rate: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE settings SET value=? WHERE key='ton_rate'", (str(rate),))
    conn.commit()
    conn.close()


def get_card_number():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key='card_number'")
    row = cur.fetchone()
    conn.close()
    if not row:
        return DEFAULT_CARD_NUMBER
    return row["value"]


def set_card_number(card: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE settings SET value=? WHERE key='card_number'", (card,))
    conn.commit()
    conn.close()


def get_mnemonic():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key='mnemonic'")
    row = cur.fetchone()
    conn.close()

    if not row:
        return MNEMONICS.strip()

    value = (row["value"] or "").strip()
    return value if value else MNEMONICS.strip()


def set_mnemonic(words: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE settings SET value=? WHERE key='mnemonic'", (words.strip(),))
    conn.commit()
    conn.close()

# =========================================================
# TELEGRAM API
# =========================================================

def tg(method: str, data=None):
    url = f"{BOT_API}/{method}"
    try:
        r = requests.post(url, data=data, timeout=60)
        return r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


def send_message(chat_id: int, text: str, reply_markup=None):
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return tg("sendMessage", data)


def edit_message(chat_id: int, message_id: int, text: str, reply_markup=None):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return tg("editMessageText", data)


def answer_callback(callback_id: str, text: str = "", show_alert: bool = False):
    data = {
        "callback_query_id": callback_id,
        "text": text,
        "show_alert": show_alert
    }
    return tg("answerCallbackQuery", data)


def set_webhook_to(url: str):
    return tg("setWebhook", {"url": url})

# =========================================================
# YORDAMCHI
# =========================================================

def valid_ton_address(address: str) -> bool:
    if not isinstance(address, str):
        return False
    address = address.strip()
    return bool(re.match(r"^(EQ|UQ)[A-Za-z0-9_-]{46,64}$", address))


def is_admin(user_id: int) -> bool:
    return int(user_id) == int(ADMIN_ID)


def format_ton(amount) -> str:
    return f"{float(amount):.9f}"


def status_text(status: str) -> str:
    mapping = {
        "waiting_payment": "💳 To‘lov kutilmoqda",
        "waiting_admin": "🕐 Admin tekshiruvda",
        "processing": "🚀 TON yuborilmoqda",
        "completed": "✅ Yakunlangan",
        "failed": "❌ Xatolik",
        "cancelled": "🚫 Bekor qilingan",
    }
    return mapping.get(status, status)


def main_keyboard():
    return {
        "keyboard": [
            [{"text": "🪙 TON sotib olish"}],
            [{"text": "📦 Buyurtmalarim"}]
        ],
        "resize_keyboard": True
    }


def admin_keyboard():
    return {
        "keyboard": [
            [{"text": "📊 Statistika"}],
            [{"text": "💰 TON narxini o‘zgartirish"}],
            [{"text": "💳 TON balans"}],
            [{"text": "💳 Karta raqamni o‘zgartirish"}],
            [{"text": "🔑 TON seed sozlash"}]
        ],
        "resize_keyboard": True
    }

# =========================================================
# USER / ORDER FUNKSIYALARI
# =========================================================

def get_or_create_user(telegram_id: int, username="", first_name=""):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()

    if not row:
        cur.execute("""
            INSERT INTO users (telegram_id, username, first_name, step, temp_wallet)
            VALUES (?, ?, ?, NULL, NULL)
        """, (telegram_id, username, first_name))
        conn.commit()
        cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
    else:
        cur.execute("""
            UPDATE users
            SET username = ?, first_name = ?
            WHERE telegram_id = ?
        """, (username, first_name, telegram_id))
        conn.commit()
        cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()

    conn.close()
    return row


def get_user(telegram_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return row


def update_user(telegram_id: int, step=None, temp_wallet=None):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
        SET step = ?, temp_wallet = ?
        WHERE telegram_id = ?
    """, (step, temp_wallet, telegram_id))
    conn.commit()
    conn.close()


def create_order(telegram_id: int, wallet_address: str, amount_ton: float, amount_uzs: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO orders (telegram_id, wallet_address, amount_ton, amount_uzs, status)
        VALUES (?, ?, ?, ?, 'waiting_payment')
    """, (telegram_id, wallet_address, amount_ton, amount_uzs))
    conn.commit()
    order_id = cur.lastrowid
    conn.close()
    return order_id


def get_order(order_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_user_orders(telegram_id: int, limit: int = 10):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM orders
        WHERE telegram_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (telegram_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows


def update_order(order_id: int, status: str, tx_data=None, error_text=None):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE orders
        SET status = ?, tx_data = ?, error_text = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (status, tx_data, error_text, order_id))
    conn.commit()
    conn.close()


def get_stats():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) as c FROM users")
    users = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) as c FROM orders")
    orders = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) as c FROM orders WHERE status='completed'")
    completed = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) as c FROM orders WHERE status='waiting_payment'")
    waiting_payment = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) as c FROM orders WHERE status='waiting_admin'")
    waiting_admin = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) as c FROM orders WHERE status='failed'")
    failed = cur.fetchone()["c"]

    cur.execute("SELECT SUM(amount_ton) as s FROM orders WHERE status='completed'")
    ton = cur.fetchone()["s"] or 0

    cur.execute("SELECT SUM(amount_uzs) as s FROM orders WHERE status='completed'")
    uzs = cur.fetchone()["s"] or 0

    conn.close()

    return {
        "users": users,
        "orders": orders,
        "completed": completed,
        "waiting_payment": waiting_payment,
        "waiting_admin": waiting_admin,
        "failed": failed,
        "ton": ton,
        "uzs": uzs
    }

# =========================================================
# TONCENTER
# =========================================================

def tc_get(path: str, params: dict) -> dict:
    url = f"{TONCENTER_BASE_URL}/{path}"

    r = requests.get(
        url,
        params=params,
        headers={"X-API-Key": TONCENTER_API_KEY},
        timeout=45
    )

    try:
        data = r.json()
    except Exception:
        r.raise_for_status()
        raise Exception(f"Toncenter GET bad response: {r.text}")

    if r.status_code >= 400:
        raise Exception(f"Toncenter GET HTTP {r.status_code}: {data}")

    if not data.get("ok"):
        raise Exception(f"Toncenter GET error: {data}")

    return data


def tc_post(path: str, payload: dict) -> dict:
    url = f"{TONCENTER_BASE_URL}/{path}"

    r = requests.post(
        url,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": TONCENTER_API_KEY
        },
        timeout=45
    )

    try:
        res = r.json()
    except Exception:
        raise Exception(f"Toncenter bad response: {r.text}")

    if r.status_code >= 400:
        raise Exception(f"Toncenter POST HTTP {r.status_code}: {res}")

    if not res.get("ok"):
        raise Exception(f"Toncenter POST error: {res}")

    return res

# =========================================================
# TON SIGNER
# =========================================================

def get_wallet():
    mnemonic_text = get_mnemonic()
    words = [w.strip() for w in mnemonic_text.split() if w.strip()]

    if len(words) not in (12, 24):
        raise RuntimeError("Mnemonic 12 yoki 24 ta so'z bo'lishi kerak")

    result = Wallets.from_mnemonics(
        mnemonics=words,
        version=WalletVersionEnum.v4r2,
        workchain=0
    )
    return result[-1]


def signer_address():
    wallet = get_wallet()
    return wallet.address.to_string(True, True, True)


def get_wallet_info(address: str) -> dict:
    return tc_get("getWalletInformation", {"address": address}).get("result", {})


def current_seqno() -> int:
    info = get_wallet_info(signer_address())
    return int(info.get("seqno", 0))


def signer_balance_ton() -> float:
    info = get_wallet_info(signer_address())
    balance_nano = int(info.get("balance", 0))
    return balance_nano / 1_000_000_000


def wait_seqno_change(old_seqno: int, max_tries: int = 15, sleep_sec: float = 1.5) -> int:
    for _ in range(max_tries):
        time.sleep(sleep_sec)
        try:
            current = current_seqno()
            if current > old_seqno:
                return current
        except Exception:
            pass
    return old_seqno


def send_ton(to_address: str, amount_ton: float, comment: str = "") -> dict:
    if not valid_ton_address(to_address):
        raise Exception("Wallet manzil noto'g'ri")

    wallet = get_wallet()
    from_address = signer_address()

    info_data = get_wallet_info(from_address)
    balance_nano = int(info_data.get("balance", 0))
    seqno = int(info_data.get("seqno", 0))

    amount_nano = int(to_nano(amount_ton, "ton"))

    reserve_nano = int(to_nano(0.05, "ton"))
    need_nano = amount_nano + reserve_nano

    if balance_nano < need_nano:
        raise Exception(
            f"Signer wallet balansida TON yetarli emas. "
            f"Have: {balance_nano / 1_000_000_000:.9f}, "
            f"Need: {need_nano / 1_000_000_000:.9f}"
        )

    transfer = wallet.create_transfer_message(
        to_addr=to_address.strip(),
        amount=amount_nano,
        seqno=seqno,
        payload=comment if comment else None,
        send_mode=3
    )

    boc = base64.b64encode(transfer["message"].to_boc(False)).decode("utf-8")

    try:
        sent = tc_post("sendBoc", {"boc": boc})
    except Exception as first_error:
        fresh_seqno = current_seqno()

        if fresh_seqno != seqno:
            transfer = wallet.create_transfer_message(
                to_addr=to_address.strip(),
                amount=amount_nano,
                seqno=fresh_seqno,
                payload=comment if comment else None,
                send_mode=3
            )
            boc = base64.b64encode(transfer["message"].to_boc(False)).decode("utf-8")
            sent = tc_post("sendBoc", {"boc": boc})
            seqno = fresh_seqno
        else:
            raise Exception(str(first_error))

    new_seqno = wait_seqno_change(seqno)

    return {
        "from_address": from_address,
        "to_address": to_address,
        "amount_ton": amount_ton,
        "old_seqno": seqno,
        "new_seqno": new_seqno,
        "toncenter": sent
    }

# =========================================================
# BUYURTMALAR MATNI
# =========================================================

def orders_text(telegram_id: int) -> str:
    rows = get_user_orders(telegram_id, limit=10)

    if not rows:
        return "📦 <b>Buyurtmalarim</b>\n\nSizda hali buyurtmalar yo‘q."

    text = "📦 <b>Buyurtmalarim</b>\n\n"
    for o in rows:
        text += (
            f"🧾 <b>#{o['id']}</b>\n"
            f"TON: <b>{format_ton(o['amount_ton'])}</b>\n"
            f"So'm: <b>{o['amount_uzs']}</b>\n"
            f"Holat: <b>{status_text(o['status'])}</b>\n"
            f"Wallet: <code>{o['wallet_address']}</code>\n"
            f"Sana: {o['created_at']}\n\n"
        )
    return text.strip()

# =========================================================
# TELEGRAM HANDLERS
# =========================================================

def handle_start(chat_id: int):
    update_user(chat_id, None, None)
    send_message(
        chat_id,
        "👋 <b>TON sotib olish botiga xush kelibsiz</b>\n\nKerakli tugmani bosing.",
        main_keyboard()
    )


def handle_buy_ton(chat_id: int):
    update_user(chat_id, "wait_wallet", None)
    send_message(chat_id, "TON wallet manzilingizni yuboring.\nMasalan: <code>UQ...</code>")


def handle_text_message(message: dict):
    chat_id = int(message["chat"]["id"])
    text = (message.get("text") or "").strip()
    username = message["from"].get("username") or ""
    first_name = message["from"].get("first_name") or ""

    get_or_create_user(chat_id, username, first_name)
    user = get_user(chat_id)
    step = user["step"]

    if text == "/start":
        handle_start(chat_id)
        return

    if text == "/admin" and is_admin(chat_id):
        send_message(chat_id, "⚙️ <b>Admin panel</b>", admin_keyboard())
        return

    if text == "📦 Buyurtmalarim":
        send_message(chat_id, orders_text(chat_id), main_keyboard())
        return

    if text == "📊 Statistika" and is_admin(chat_id):
        stats = get_stats()
        send_message(
            chat_id,
            f"📊 <b>Statistika</b>\n\n"
            f"👥 Users: <b>{stats['users']}</b>\n"
            f"📦 Orders: <b>{stats['orders']}</b>\n"
            f"✅ Completed: <b>{stats['completed']}</b>\n"
            f"💳 Waiting payment: <b>{stats['waiting_payment']}</b>\n"
            f"🕐 Waiting admin: <b>{stats['waiting_admin']}</b>\n"
            f"❌ Failed: <b>{stats['failed']}</b>\n"
            f"🪙 Jami yuborilgan TON: <b>{format_ton(stats['ton'])}</b>\n"
            f"💰 Jami so'm: <b>{stats['uzs']}</b>"
        )
        return

    if text == "💰 TON narxini o‘zgartirish" and is_admin(chat_id):
        update_user(chat_id, "set_rate", None)
        send_message(chat_id, f"Yangi TON narxini kiriting.\nHozirgi narx: <b>{get_ton_rate()}</b> so'm")
        return

    if text == "💳 Karta raqamni o‘zgartirish" and is_admin(chat_id):
        update_user(chat_id, "set_card", None)
        send_message(chat_id, f"Yangi karta raqamni kiriting.\nHozirgi karta:\n<code>{get_card_number()}</code>")
        return

    if text == "💳 TON balans" and is_admin(chat_id):
        try:
            address = signer_address()
            balance = signer_balance_ton()
            send_message(
                chat_id,
                f"💳 <b>Signer wallet</b>\n\n"
                f"Wallet:\n<code>{address}</code>\n\n"
                f"Balance:\n<b>{balance:.9f} TON</b>"
            )
        except Exception as e:
            send_message(chat_id, f"❌ Balansni olishda xatolik:\n<code>{str(e)}</code>")
        return

    if text == "🔑 TON seed sozlash" and is_admin(chat_id):
        update_user(chat_id, "set_seed", None)
        send_message(
            chat_id,
            "24 ta yoki 12 ta seed so‘zni bitta xabarda yuboring.\n\n"
            "Misol:\n<code>word1 word2 word3 ...</code>"
        )
        return

    if step == "set_rate" and is_admin(chat_id):
        try:
            rate = int(text)
        except Exception:
            send_message(chat_id, "❌ Noto‘g‘ri qiymat. Masalan: <code>17000</code>")
            return

        if rate <= 0:
            send_message(chat_id, "❌ Narx 0 dan katta bo‘lishi kerak.")
            return

        set_ton_rate(rate)
        update_user(chat_id, None, None)
        send_message(chat_id, f"✅ TON narxi yangilandi: <b>{rate}</b> so'm")
        return

    if step == "set_card" and is_admin(chat_id):
        card = text.strip()
        if len(card) < 8:
            send_message(chat_id, "❌ Karta raqam noto‘g‘ri.")
            return

        set_card_number(card)
        update_user(chat_id, None, None)
        send_message(chat_id, f"✅ Karta raqam yangilandi:\n<code>{card}</code>")
        return

    if step == "set_seed" and is_admin(chat_id):
        words = [w.strip() for w in text.split() if w.strip()]

        if len(words) not in (12, 24):
            send_message(chat_id, "❌ Seed noto‘g‘ri. 12 yoki 24 ta so‘z yuboring.")
            return

        seed_text = " ".join(words)

        try:
            set_mnemonic(seed_text)
            address = signer_address()
            balance = signer_balance_ton()
            update_user(chat_id, None, None)

            send_message(
                chat_id,
                f"✅ <b>Seed saqlandi</b>\n\n"
                f"Wallet:\n<code>{address}</code>\n\n"
                f"Balance:\n<b>{balance:.9f} TON</b>"
            )
        except Exception as e:
            send_message(chat_id, f"❌ Seed saqlashda yoki wallet tekshirishda xatolik:\n<code>{str(e)}</code>")
        return

    if text == "🪙 TON sotib olish":
        handle_buy_ton(chat_id)
        return

    if step == "wait_wallet":
        if not valid_ton_address(text):
            send_message(chat_id, "❌ Wallet noto‘g‘ri. Qayta yuboring.")
            return

        update_user(chat_id, "wait_amount", text)
        send_message(chat_id, "Necha TON olmoqchisiz?\nMasalan: <code>0.1</code>")
        return

    if step == "wait_amount":
        try:
            amount_ton = float(text.replace(",", "."))
        except Exception:
            send_message(chat_id, "❌ Miqdor noto‘g‘ri. Masalan: <code>0.1</code>")
            return

        if amount_ton <= 0:
            send_message(chat_id, "❌ Miqdor 0 dan katta bo‘lishi kerak.")
            return

        wallet = user["temp_wallet"]
        if not wallet:
            update_user(chat_id, None, None)
            send_message(chat_id, "⚠️ Xatolik. /start bosing.")
            return

        amount_ton = round(amount_ton, 9)
        amount_uzs = int(round(amount_ton * get_ton_rate()))
        order_id = create_order(chat_id, wallet, amount_ton, amount_uzs)

        update_user(chat_id, None, None)

        send_message(
            chat_id,
            f"🧾 <b>Buyurtma yaratildi</b>\n\n"
            f"Order: <b>#{order_id}</b>\n"
            f"Wallet: <code>{wallet}</code>\n"
            f"Miqdor: <b>{amount_ton:.9f} TON</b>\n"
            f"To'lov: <b>{amount_uzs} so'm</b>\n\n"
            f"💳 <b>Admin karta:</b> <code>{get_card_number()}</code>\n\n"
            f"To'lov qilganingizdan keyin pastdagi tugmani bosing.",
            {
                "inline_keyboard": [
                    [{"text": "✅ To‘ladim", "callback_data": f"paid_{order_id}"}]
                ]
            }
        )
        return

    send_message(chat_id, "Tushunmadim. /start bosing.", main_keyboard())


def handle_callback(callback: dict):
    callback_id = callback["id"]
    from_id = int(callback["from"]["id"])
    data = callback.get("data", "")
    message = callback.get("message")

    if data.startswith("paid_"):
        order_id = int(data.split("_")[1])
        order = get_order(order_id)

        if not order:
            answer_callback(callback_id, "Order topilmadi", True)
            return

        if int(order["telegram_id"]) != from_id:
            answer_callback(callback_id, "Bu sizning order emas", True)
            return

        if order["status"] != "waiting_payment":
            answer_callback(callback_id, "Bu order ishlangan", True)
            return

        update_order(order_id, "waiting_admin")
        answer_callback(callback_id, "Admin tekshiradi")

        if message:
            try:
                edit_message(
                    int(message["chat"]["id"]),
                    int(message["message_id"]),
                    f"🧾 <b>Buyurtma yaratildi</b>\n\n"
                    f"Order: <b>#{order['id']}</b>\n"
                    f"Wallet: <code>{order['wallet_address']}</code>\n"
                    f"Miqdor: <b>{float(order['amount_ton']):.9f} TON</b>\n"
                    f"To'lov: <b>{order['amount_uzs']} so'm</b>\n\n"
                    f"🕐 <b>Holat:</b> Admin tekshiruvda"
                )
            except Exception:
                pass

        send_message(
            ADMIN_ID,
            f"💳 <b>Yangi buyurtma</b>\n\n"
            f"Order: <b>#{order['id']}</b>\n"
            f"User ID: <code>{order['telegram_id']}</code>\n"
            f"Wallet: <code>{order['wallet_address']}</code>\n"
            f"Miqdor: <b>{float(order['amount_ton']):.9f} TON</b>\n"
            f"To'lov: <b>{order['amount_uzs']} so'm</b>",
            {
                "inline_keyboard": [[
                    {"text": "✅ Tasdiqlash", "callback_data": f"approve_{order_id}"},
                    {"text": "❌ Bekor qilish", "callback_data": f"reject_{order_id}"}
                ]]
            }
        )
        return

    if data.startswith("approve_"):
        if not is_admin(from_id):
            answer_callback(callback_id, "Siz admin emassiz", True)
            return

        order_id = int(data.split("_")[1])
        order = get_order(order_id)

        if not order:
            answer_callback(callback_id, "Order topilmadi", True)
            return

        if order["status"] != "waiting_admin":
            answer_callback(callback_id, "Order allaqachon ishlangan", True)
            return

        update_order(order_id, "processing")
        answer_callback(callback_id, "TON yuborilmoqda...")

        try:
            result = send_ton(
                to_address=order["wallet_address"],
                amount_ton=float(order["amount_ton"]),
                comment=f"Order #{order_id}"
            )

            update_order(order_id, "completed", tx_data=json.dumps(result, ensure_ascii=False), error_text=None)

            send_message(
                int(order["telegram_id"]),
                f"✅ <b>TON yuborildi</b>\n\n"
                f"Order: <b>#{order_id}</b>\n"
                f"Miqdor: <b>{float(order['amount_ton']):.9f} TON</b>\n"
                f"Wallet: <code>{order['wallet_address']}</code>\n"
                f"Old seqno: <b>{result['old_seqno']}</b>\n"
                f"New seqno: <b>{result['new_seqno']}</b>"
            )

            send_message(
                ADMIN_ID,
                f"✅ Order #{order_id} bo'yicha TON yuborildi.\n"
                f"Old seqno: {result['old_seqno']}\n"
                f"New seqno: {result['new_seqno']}"
            )

        except Exception as e:
            update_order(order_id, "failed", tx_data=None, error_text=str(e))
            send_message(ADMIN_ID, f"❌ TON yuborishda xatolik:\n{str(e)}")
            send_message(int(order["telegram_id"]), f"❌ TON yuborishda xatolik:\n{str(e)}")

        return

    if data.startswith("reject_"):
        if not is_admin(from_id):
            answer_callback(callback_id, "Siz admin emassiz", True)
            return

        order_id = int(data.split("_")[1])
        order = get_order(order_id)

        if order:
            update_order(order_id, "cancelled")
            send_message(int(order["telegram_id"]), "❌ Buyurtma admin tomonidan bekor qilindi")

        answer_callback(callback_id, "Bekor qilindi")
        return

# =========================================================
# ROUTES
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return "TON Python bot is running"


@app.route("/info", methods=["GET"])
def info():
    try:
        address = signer_address()
        info_data = get_wallet_info(address)
        mnemonic = get_mnemonic()
        return jsonify({
            "status": "ok",
            "signer_wallet": address,
            "balance_ton": signer_balance_ton(),
            "wallet_info": info_data,
            "ton_rate": get_ton_rate(),
            "card_number": get_card_number(),
            "mnemonic_words_count": len(mnemonic.split())
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route("/setwebhook", methods=["GET"])
def setwebhook():
    host = request.host_url.rstrip("/")
    url = f"{host}/webhook/{BOT_TOKEN}"
    result = set_webhook_to(url)
    return jsonify(result)


@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}

    try:
        if "message" in update:
            handle_text_message(update["message"])
        elif "callback_query" in update:
            handle_callback(update["callback_query"])
    except Exception as e:
        try:
            send_message(ADMIN_ID, f"❌ Webhook xatolik:\n{str(e)}")
        except Exception:
            pass

    return "ok"

# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
