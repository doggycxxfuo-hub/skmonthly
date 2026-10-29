import asyncio
import html
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps

import psycopg
from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ---------------------------------------------------------------- config
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
# comma separated Telegram user IDs, e.g. "123456789,987654321"
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
DEFAULT_DAYS = 30
LOCAL_TZ = timezone(timedelta(hours=7))  # Cambodia / Thailand time (UTC+7)
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid typos
MAX_FIELD_LEN = 100

esc = html.escape  # user text goes into HTML messages, always escape it

# ---------------------------------------------------------- health server
web = Flask(__name__)


@web.route("/")
def health():
    return "Bot is running", 200


def run_web():
    web.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


# --------------------------------------------------------------- database
def connect():
    return psycopg.connect(DATABASE_URL, connect_timeout=15)


def init_db():
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS codes (
                code        TEXT PRIMARY KEY,
                days        INTEGER NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL,
                created_by  BIGINT NOT NULL,
                redeemed_by BIGINT,
                redeemed_at TIMESTAMPTZ
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    BIGINT PRIMARY KEY,
                username   TEXT,
                expires_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        # accounts the admin creates (username + password handed out with a code)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id         SERIAL PRIMARY KEY,
                username   TEXT NOT NULL,
                password   TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        # copy of the credentials each user received (survives account deletion)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_accounts (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT NOT NULL,
                username    TEXT NOT NULL,
                password    TEXT NOT NULL,
                redeemed_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        conn.execute("ALTER TABLE codes ADD COLUMN IF NOT EXISTS account_id INTEGER")


def new_code() -> str:
    parts = ["".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(3)]
    return "SK-" + "-".join(parts)


def db_generate(
    admin_id: int, days: int, count: int, account_id: int | None = None
) -> list[str]:
    now = datetime.now(timezone.utc)
    codes = [new_code() for _ in range(count)]
    with connect() as conn:
        for c in codes:
            conn.execute(
                "INSERT INTO codes (code, days, created_at, created_by, account_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (c, days, now, admin_id, account_id),
            )
    return codes


def db_redeem(code: str, user_id: int, username: str | None):
    """Returns (days, new_expiry, creds) or None if the code is invalid / used.
    creds is (username, password) when the code is linked to an account."""
    now = datetime.now(timezone.utc)
    with connect() as conn:
        # atomic: only one person can ever claim a code
        row = conn.execute(
            "UPDATE codes SET redeemed_by = %s, redeemed_at = %s "
            "WHERE code = %s AND redeemed_by IS NULL RETURNING days, account_id",
            (user_id, now, code),
        ).fetchone()
        if not row:
            return None
        days, account_id = row

        current = conn.execute(
            "SELECT expires_at FROM users WHERE user_id = %s", (user_id,)
        ).fetchone()
        # still active -> extend from current expiry, otherwise start from now
        base = current[0] if current and current[0] > now else now
        new_expiry = base + timedelta(days=days)

        conn.execute(
            "INSERT INTO users (user_id, username, expires_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE "
            "SET username = EXCLUDED.username, expires_at = EXCLUDED.expires_at",
            (user_id, username, new_expiry),
        )

        creds = None
        if account_id is not None:
            acc = conn.execute(
                "SELECT username, password FROM accounts WHERE id = %s", (account_id,)
            ).fetchone()
            if acc:
                conn.execute(
                    "INSERT INTO user_accounts (user_id, username, password, redeemed_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (user_id, acc[0], acc[1], now),
                )
                creds = (acc[0], acc[1])
    return days, new_expiry, creds


def db_get_expiry(user_id: int):
    with connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM users WHERE user_id = %s", (user_id,)
        ).fetchone()
    return row[0] if row else None


def db_user_credentials(user_id: int):
    with connect() as conn:
        return conn.execute(
            "SELECT username, password FROM user_accounts "
            "WHERE user_id = %s ORDER BY redeemed_at",
            (user_id,),
        ).fetchall()


def db_unused_codes(limit: int = 50):
    with connect() as conn:
        return conn.execute(
            "SELECT c.code, c.days, a.username FROM codes c "
            "LEFT JOIN accounts a ON a.id = c.account_id "
            "WHERE c.redeemed_by IS NULL ORDER BY c.created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()


def db_revoke(code: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM codes WHERE code = %s AND redeemed_by IS NULL", (code,)
        )
        return cur.rowcount > 0


def db_stats():
    now = datetime.now(timezone.utc)
    with connect() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM users WHERE expires_at > %s", (now,)
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        unused = conn.execute(
            "SELECT COUNT(*) FROM codes WHERE redeemed_by IS NULL"
        ).fetchone()[0]
        accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    return active, total, unused, accounts


def db_find_user(query: str):
    """Find a user by Telegram ID or @username. Returns (id, username, expiry)."""
    q = query.strip().lstrip("@")
    uid = int(q) if q.isdigit() else None
    with connect() as conn:
        return conn.execute(
            "SELECT user_id, username, expires_at FROM users "
            "WHERE user_id = %s OR LOWER(username) = LOWER(%s)",
            (uid, q),
        ).fetchone()


# ---- accounts (admin)
def db_add_account(username: str, password: str) -> int:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO accounts (username, password, created_at) "
            "VALUES (%s, %s, %s) RETURNING id",
            (username, password, datetime.now(timezone.utc)),
        ).fetchone()[0]


def db_list_accounts(limit: int = 50):
    """(id, username, unused_codes, redeemed_codes) newest first."""
    with connect() as conn:
        return conn.execute(
            "SELECT a.id, a.username, "
            "COUNT(c.code) FILTER (WHERE c.redeemed_by IS NULL), "
            "COUNT(c.code) FILTER (WHERE c.redeemed_by IS NOT NULL) "
            "FROM accounts a LEFT JOIN codes c ON c.account_id = a.id "
            "GROUP BY a.id ORDER BY a.id DESC LIMIT %s",
            (limit,),
        ).fetchall()


def db_get_account(account_id: int):
    """(id, username, password, unused_codes, redeemed_codes) or None."""
    with connect() as conn:
        return conn.execute(
            "SELECT a.id, a.username, a.password, "
            "COUNT(c.code) FILTER (WHERE c.redeemed_by IS NULL), "
            "COUNT(c.code) FILTER (WHERE c.redeemed_by IS NOT NULL) "
            "FROM accounts a LEFT JOIN codes c ON c.account_id = a.id "
            "WHERE a.id = %s GROUP BY a.id",
            (account_id,),
        ).fetchone()


def db_delete_account(account_id: int) -> bool:
    with connect() as conn:
        conn.execute(
            "DELETE FROM codes WHERE account_id = %s AND redeemed_by IS NULL",
            (account_id,),
        )
        cur = conn.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
        return cur.rowcount > 0


# ======================================================================
#  MESSAGES (Khmer) - edit any text here, the code below doesn't need to change
#  Keep the {placeholders} and the <code>...</code> tags as they are.
# ======================================================================
M = {
    # buttons
    "btn_account": "👤 គណនី",
    "btn_activate": "🔑 ធ្វើឱ្យសកម្ម",
    "btn_admin": "🛠 ផ្ទាំងអ្នកគ្រប់គ្រង",
    "btn_cancel": "✖ បោះបង់",
    "btn_back": "⬅ ត្រឡប់ក្រោយ",
    "btn_back_list": "⬅ ត្រឡប់ទៅបញ្ជីគណនី",
    "btn_gen": "🎟 {days} ថ្ងៃ",
    "btn_add_account": "➕ បង្កើតគណនី",
    "btn_accounts": "📂 គណនីទាំងអស់",
    "btn_codes": "📋 កូដមិនទាន់ប្រើ",
    "btn_user": "🔍 ពិនិត្យអ្នកប្រើ",
    "btn_revoke": "🗑 លុបកូដ",
    "btn_delete_account": "🗑 លុបគណនី",
    "btn_yes_delete": "✅ បាទ/ចាស លុប",
    "btn_no": "↩ ទេ",
    # general
    "welcome": "សូមស្វាគមន៍! សូមជ្រើសរើសជម្រើស៖",
    "choose": "សូមជ្រើសរើសជម្រើស៖",
    "cancelled": "បានបោះបង់។",
    "admins_only": "ពាក្យបញ្ជានេះសម្រាប់តែអ្នកគ្រប់គ្រងប៉ុណ្ណោះ។",
    "myid": "លេខសម្គាល់ Telegram របស់អ្នក: {id}",
    # time
    "expired_word": "ផុតកំណត់ហើយ",
    "remaining_fmt": "{d} ថ្ងៃ {h} ម៉ោង {m} នាទី",
    # account screen
    "acc_title": "👤 គណនី",
    "acc_username": "ឈ្មោះអ្នកប្រើ: {v}",
    "acc_id": "លេខសម្គាល់អ្នកប្រើ (ID): {v}",
    "acc_not_active": "ស្ថានភាព: មិនទាន់ធ្វើឱ្យសកម្ម",
    "acc_expired": "ស្ថានភាព: ផុតកំណត់",
    "acc_expired_on": "ផុតកំណត់នៅ: {v}",
    "acc_active": "ស្ថានភាព: សកម្ម",
    "acc_expires": "ផុតកំណត់: {v}",
    "acc_remaining": "ពេលវេលានៅសល់: {v}",
    "creds_block": (
        "🔐 ព័ត៌មានគណនីរបស់អ្នក\n"
        "ឈ្មោះអ្នកប្រើ: <code>{u}</code>\n"
        "ពាក្យសម្ងាត់: <code>{p}</code>"
    ),
    # activation
    "ask_code": "សូមផ្ញើកូដធ្វើឱ្យសកម្មរបស់អ្នកឥឡូវនេះ។\nឧទាហរណ៍: SK-XXXX-XXXX-XXXX",
    "retry_code": "\nសូមផ្ញើកូដម្ដងទៀត ឬចុចបោះបង់។",
    "bad_code": "កូដមិនត្រឹមត្រូវ ឬត្រូវបានប្រើរួចហើយ។",
    "activated": "✅ ធ្វើឱ្យសកម្មបានជោគជ័យ! បន្ថែម {days} ថ្ងៃ។",
    "redeem_usage": "របៀបប្រើ: /redeem SK-XXXX-XXXX-XXXX",
    "need_activation": (
        "សិទ្ធិរបស់អ្នកបានផុតកំណត់ ឬមិនទាន់ធ្វើឱ្យសកម្ម។\n"
        "ប្រើ /redeem កូដរបស់អ្នក ដើម្បីធ្វើឱ្យសកម្ម។"
    ),
    "status_none": "មិនទាន់ធ្វើឱ្យសកម្ម។ ប្រើ /redeem កូដរបស់អ្នក",
    "status_expired": (
        "សិទ្ធិរបស់អ្នកបានផុតកំណត់នៅ {date}។\n"
        "សូមស្នើសុំកូដថ្មីពីអ្នកគ្រប់គ្រង រួចប្រើ /redeem។"
    ),
    "status_active": "សកម្ម\nផុតកំណត់: {date}\nពេលវេលានៅសល់: {left}",
    "vip_ok": "អ្នកមានការជាវសកម្ម។ សូមស្វាគមន៍!",
    # admin panel
    "panel": (
        "🛠 ផ្ទាំងអ្នកគ្រប់គ្រង\n"
        "គណនីសរុប: {accounts}\n"
        "អ្នកប្រើសកម្ម: {active}\n"
        "អ្នកប្រើសរុប: {total}\n"
        "កូដមិនទាន់ប្រើ: {unused}\n\n"
        "សូមជ្រើសរើសសកម្មភាព៖"
    ),
    "gen_many": (
        "បានបង្កើតកូដចំនួន {count} (មួយកូដ {days} ថ្ងៃ):\n\n{body}\n\n"
        "ចុចលើកូដដើម្បីចម្លង រួចផ្ញើទៅអ្នកប្រើ។"
    ),
    "gen_usage": "របៀបប្រើ: /gen [ចំនួនថ្ងៃ] [ចំនួនកូដ]",
    "gen_range": "ចំនួនថ្ងៃត្រូវតែពី 1 ដល់ 3650 និងចំនួនកូដពី 1 ដល់ 20។",
    "no_codes": "គ្មានកូដដែលមិនទាន់ប្រើទេ។",
    "codes_list": "កូដមិនទាន់ប្រើ ({n}):\n\n{body}",
    "ask_user": "សូមផ្ញើលេខសម្គាល់ Telegram ឬ @username របស់អ្នកប្រើ។",
    "ask_revoke": "សូមផ្ញើកូដដែលអ្នកចង់លុប (SK-XXXX-XXXX-XXXX)។",
    "user_not_found": "រកមិនឃើញអ្នកប្រើទេ។ សូមផ្ញើ ID / @username ផ្សេង ឬចុចបោះបង់។",
    "user_info": "👤 អ្នកប្រើ\nID: {id}\nឈ្មោះអ្នកប្រើ: {name}\n{state}",
    "user_state_active": "សកម្ម\nផុតកំណត់: {date}\nនៅសល់: {left}",
    "user_state_expired": "ផុតកំណត់នៅ {date}",
    "revoked": "បានលុបកូដរួចរាល់។",
    "revoke_fail": "រកមិនឃើញកូដ ឬកូដត្រូវបានប្រើរួចហើយ។",
    "revoke_usage": "របៀបប្រើ: /revoke SK-XXXX-XXXX-XXXX",
    "user_usage": "របៀបប្រើ: /user TELEGRAM_ID",
    "user_never": "អ្នកប្រើនេះមិនទាន់ធ្វើឱ្យសកម្មកូដណាមួយទេ។",
    "user_cmd_info": "ផុតកំណត់: {date}\nនៅសល់: {left}",
    # admin: accounts (username + password)
    "ask_acc_username": "សូមផ្ញើ ឈ្មោះអ្នកប្រើ (username) សម្រាប់គណនីថ្មី។",
    "ask_acc_password": "សូមផ្ញើ ពាក្យសម្ងាត់ (password) សម្រាប់គណនីនេះ។",
    "acc_empty_input": "មិនអាចទទេបានទេ។ សូមផ្ញើម្ដងទៀត ឬចុចបោះបង់។",
    "acc_too_long": "វែងពេក (អតិបរមា 100 តួអក្សរ)។ សូមផ្ញើម្ដងទៀត ឬចុចបោះបង់។",
    "acc_created": "✅ បានបង្កើតគណនីថ្មី!\n\n",
    "acc_detail": (
        "🔹 គណនី\n"
        "ឈ្មោះអ្នកប្រើ: <code>{u}</code>\n"
        "ពាក្យសម្ងាត់: <code>{p}</code>\n"
        "កូដមិនទាន់ប្រើ: {unused}\n"
        "កូដប្រើរួច: {used}\n\n"
        "ជ្រើសរើសរយៈពេល ដើម្បីបង្កើតកូដធ្វើឱ្យសកម្មសម្រាប់គណនីនេះ៖"
    ),
    "acc_list_title": "📂 គណនីទាំងអស់ ({n})\nចុចលើគណនីមួយ ដើម្បីបង្កើតកូដ៖\n(🎟 = កូដមិនទាន់ប្រើ, ✅ = ប្រើរួច)",
    "acc_list_empty": "មិនទាន់មានគណនីទេ។ ចុច «បង្កើតគណនី» ដើម្បីបន្ថែម។",
    "acc_not_found": "រកមិនឃើញគណនីនេះទេ។",
    "acc_confirm_delete": (
        "⚠️ តើអ្នកប្រាកដថាចង់លុបគណនី <code>{u}</code> មែនទេ?\n"
        "កូដដែលមិនទាន់ប្រើរបស់គណនីនេះនឹងត្រូវលុបដែរ។ "
        "អ្នកប្រើដែលបានទទួលរួចហើយនឹងមិនរងផលប៉ះពាល់ទេ។"
    ),
    "acc_deleted": "បានលុបគណនីរួចរាល់។",
    "gen_for_account": (
        "🎟 កូដថ្មី {days} ថ្ងៃ សម្រាប់គណនី <code>{u}</code>:\n\n"
        "<code>{code}</code>\n\n"
        "ចុចលើកូដដើម្បីចម្លង រួចផ្ញើទៅអ្នកប្រើ។ "
        "នៅពេលអ្នកប្រើធ្វើឱ្យសកម្ម គាត់នឹងទទួលបានឈ្មោះអ្នកប្រើ និងពាក្យសម្ងាត់។"
    ),
}


# ---------------------------------------------------------------- helpers
def fmt_remaining(expiry: datetime) -> str:
    delta = expiry - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return M["expired_word"]
    hours, rem = divmod(delta.seconds, 3600)
    return M["remaining_fmt"].format(d=delta.days, h=hours, m=rem // 60)


def fmt_date(dt: datetime) -> str:
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M (UTC+7)")


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text(M["admins_only"])
            return
        return await func(update, context)

    return wrapper


def active_only(func):
    """Use on any command that should only work for users with a valid code."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        expiry = await asyncio.to_thread(db_get_expiry, update.effective_user.id)
        if not expiry or expiry <= datetime.now(timezone.utc):
            await update.message.reply_text(M["need_activation"])
            return
        return await func(update, context)

    return wrapper


# ------------------------------------------------------------------ menus
def main_menu(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(M["btn_account"], callback_data="account"),
            InlineKeyboardButton(M["btn_activate"], callback_data="activate"),
        ]
    ]
    if user_id in ADMIN_IDS:
        rows.append([InlineKeyboardButton(M["btn_admin"], callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(M["btn_add_account"], callback_data="ax_add"),
                InlineKeyboardButton(M["btn_accounts"], callback_data="ax_list"),
            ],
            [
                InlineKeyboardButton(M["btn_codes"], callback_data="adm_codes"),
                InlineKeyboardButton(M["btn_user"], callback_data="adm_user"),
            ],
            [InlineKeyboardButton(M["btn_revoke"], callback_data="adm_revoke")],
            [InlineKeyboardButton(M["btn_back"], callback_data="back")],
        ]
    )


def account_menu(acc_id: int) -> InlineKeyboardMarkup:
    gens = [
        InlineKeyboardButton(
            M["btn_gen"].format(days=d), callback_data=f"ax_gen_{acc_id}_{d}"
        )
        for d in (7, 30, 90)
    ]
    return InlineKeyboardMarkup(
        [
            gens,
            [
                InlineKeyboardButton(
                    M["btn_delete_account"], callback_data=f"ax_del_{acc_id}"
                )
            ],
            [InlineKeyboardButton(M["btn_back_list"], callback_data="ax_list")],
        ]
    )


def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(M["btn_cancel"], callback_data="cancel")]]
    )


def clear_pending(context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "awaiting_code",
        "admin_action",
        "new_acc_username",
        "prompt_id",
        "tries",
    ):
        context.user_data.pop(key, None)


async def admin_panel_text() -> str:
    active, total, unused, accounts = await asyncio.to_thread(db_stats)
    return M["panel"].format(
        active=active, total=total, unused=unused, accounts=accounts
    )


async def account_text(user) -> str:
    expiry = await asyncio.to_thread(db_get_expiry, user.id)
    name = f"@{user.username}" if user.username else (user.full_name or "-")
    lines = [
        M["acc_title"],
        M["acc_username"].format(v=esc(name)),
        M["acc_id"].format(v=user.id),
    ]
    now = datetime.now(timezone.utc)
    if not expiry:
        lines.append(M["acc_not_active"])
    elif expiry <= now:
        lines += [M["acc_expired"], M["acc_expired_on"].format(v=fmt_date(expiry))]
    else:
        lines += [
            M["acc_active"],
            M["acc_expires"].format(v=fmt_date(expiry)),
            M["acc_remaining"].format(v=fmt_remaining(expiry)),
        ]
        # the username/password is only shown while the access is active
        creds = await asyncio.to_thread(db_user_credentials, user.id)
        for u, p in creds:
            lines += ["", M["creds_block"].format(u=esc(u), p=esc(p))]
    return "\n".join(lines)


async def do_redeem(code: str, user) -> tuple[str, bool]:
    """Shared by /redeem and the Activate button. Returns (HTML message, success)."""
    result = await asyncio.to_thread(db_redeem, code, user.id, user.username)
    if result is None:
        return M["bad_code"], False
    days, expiry, creds = result
    lines = [
        M["activated"].format(days=days),
        M["acc_expires"].format(v=fmt_date(expiry)),
        M["acc_remaining"].format(v=fmt_remaining(expiry)),
    ]
    if creds:
        lines += ["", M["creds_block"].format(u=esc(creds[0]), p=esc(creds[1]))]
    return "\n".join(lines), True


# ------------------------------------------------ in-place message updates
async def render(bot, chat_id, message_id, text, markup=None, mode=None) -> int:
    """Edit the bot's existing message in place so the chat doesn't fill up.
    Falls back to sending a new message if the old one can't be edited.
    Returns the id of the message that now shows the text."""
    if message_id is not None:
        try:
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=markup,
                parse_mode=mode,
            )
            return message_id
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return message_id  # same content, nothing to do
            log.warning("edit failed, sending a new message instead: %s", e)
    sent = await bot.send_message(
        chat_id, text, reply_markup=markup, parse_mode=mode
    )
    return sent.message_id


def fail_mark(context: ContextTypes.DEFAULT_TYPE) -> str:
    """' (2)', ' (3)'... so repeated identical errors still visibly change."""
    n = context.user_data["tries"] = context.user_data.get("tries", 0) + 1
    return f" ({n})" if n > 1 else ""


# ---------------------------------------------------- admin account screens
async def show_account_list(bot, chat_id: int, message_id: int | None):
    rows = await asyncio.to_thread(db_list_accounts)
    if not rows:
        await render(bot, chat_id, message_id, M["acc_list_empty"], admin_menu())
        return
    buttons = [
        [
            InlineKeyboardButton(
                f"👤 {name[:30]}   🎟{unused} ✅{used}",
                callback_data=f"ax_open_{acc_id}",
            )
        ]
        for acc_id, name, unused, used in rows
    ]
    buttons.append(
        [
            InlineKeyboardButton(M["btn_add_account"], callback_data="ax_add"),
            InlineKeyboardButton(M["btn_back"], callback_data="admin"),
        ]
    )
    await render(
        bot,
        chat_id,
        message_id,
        M["acc_list_title"].format(n=len(rows)),
        InlineKeyboardMarkup(buttons),
    )


async def show_account(
    bot, chat_id: int, message_id: int | None, acc_id: int, prefix: str = ""
):
    acc = await asyncio.to_thread(db_get_account, acc_id)
    if not acc:
        await render(bot, chat_id, message_id, M["acc_not_found"], admin_menu())
        return
    _, username, password, unused, used = acc
    await render(
        bot,
        chat_id,
        message_id,
        prefix
        + M["acc_detail"].format(
            u=esc(username), p=esc(password), unused=unused, used=used
        ),
        account_menu(acc_id),
        ParseMode.HTML,
    )


# --------------------------------------------------------- user commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_pending(context)
    await update.message.reply_text(
        M["welcome"], reply_markup=main_menu(update.effective_user.id)
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(M["myid"].format(id=update.effective_user.id))


async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(M["redeem_usage"])
        return
    code = context.args[0].strip().upper()
    message, _ = await do_redeem(code, update.effective_user)
    await update.message.reply_text(
        message,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(update.effective_user.id),
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expiry = await asyncio.to_thread(db_get_expiry, update.effective_user.id)
    if not expiry:
        await update.message.reply_text(M["status_none"])
        return
    if expiry <= datetime.now(timezone.utc):
        await update.message.reply_text(
            M["status_expired"].format(date=fmt_date(expiry))
        )
        return
    await update.message.reply_text(
        M["status_active"].format(date=fmt_date(expiry), left=fmt_remaining(expiry))
    )


@active_only
async def vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # example of a protected command - replace with your real feature
    await update.message.reply_text(M["vip_ok"])


# -------------------------------------------------------- button handling
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = query.data
    msg = query.message
    bot, chat_id, mid = context.bot, msg.chat_id, msg.message_id

    async def show(text, markup=None, mode=None):
        """Replace the text of the message that holds the pressed button."""
        return await render(bot, chat_id, mid, text, markup, mode)

    # ---- admin buttons (always re-check the user is an admin)
    if data == "admin" or data.startswith(("adm_", "ax_")):
        if user.id not in ADMIN_IDS:
            await show(M["admins_only"], main_menu(user.id))
            return
        clear_pending(context)

        if data == "admin":
            await show(await admin_panel_text(), admin_menu())

        elif data == "adm_codes":
            rows = await asyncio.to_thread(db_unused_codes)
            if not rows:
                await show(M["no_codes"], admin_menu())
            else:
                body = "\n".join(
                    f"<code>{c}</code> ({d} ថ្ងៃ{' · ' + esc(a) if a else ''})"
                    for c, d, a in rows
                )
                await show(
                    M["codes_list"].format(n=len(rows), body=body),
                    admin_menu(),
                    ParseMode.HTML,
                )

        elif data == "adm_user":
            context.user_data["admin_action"] = "check_user"
            context.user_data["prompt_id"] = mid
            await show(M["ask_user"], cancel_menu())

        elif data == "adm_revoke":
            context.user_data["admin_action"] = "revoke"
            context.user_data["prompt_id"] = mid
            await show(M["ask_revoke"], cancel_menu())

        elif data == "ax_add":
            context.user_data["admin_action"] = "acc_username"
            context.user_data["prompt_id"] = mid
            await show(M["ask_acc_username"], cancel_menu())

        elif data == "ax_list":
            await show_account_list(bot, chat_id, mid)

        elif data.startswith("ax_open_"):
            await show_account(bot, chat_id, mid, int(data.rsplit("_", 1)[1]))

        elif data.startswith("ax_gen_"):
            _, _, acc_id, days = data.split("_")
            acc_id, days = int(acc_id), int(days)
            acc = await asyncio.to_thread(db_get_account, acc_id)
            if not acc:
                await show(M["acc_not_found"], admin_menu())
                return
            codes = await asyncio.to_thread(db_generate, user.id, days, 1, acc_id)
            # a NEW message on purpose: every generated code stays in the chat
            await bot.send_message(
                chat_id,
                M["gen_for_account"].format(days=days, u=esc(acc[1]), code=codes[0]),
                parse_mode=ParseMode.HTML,
            )

        elif data.startswith("ax_del_"):
            acc_id = int(data.rsplit("_", 1)[1])
            acc = await asyncio.to_thread(db_get_account, acc_id)
            if not acc:
                await show(M["acc_not_found"], admin_menu())
                return
            await show(
                M["acc_confirm_delete"].format(u=esc(acc[1])),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                M["btn_yes_delete"], callback_data=f"ax_delok_{acc_id}"
                            ),
                            InlineKeyboardButton(
                                M["btn_no"], callback_data=f"ax_open_{acc_id}"
                            ),
                        ]
                    ]
                ),
                ParseMode.HTML,
            )

        elif data.startswith("ax_delok_"):
            acc_id = int(data.rsplit("_", 1)[1])
            ok = await asyncio.to_thread(db_delete_account, acc_id)
            await show(
                M["acc_deleted"] if ok else M["acc_not_found"], admin_menu()
            )
        return

    # ---- normal user buttons
    if data == "account":
        clear_pending(context)
        await show(await account_text(user), main_menu(user.id), ParseMode.HTML)
    elif data == "activate":
        clear_pending(context)
        context.user_data["awaiting_code"] = True
        context.user_data["prompt_id"] = mid
        await show(M["ask_code"], cancel_menu())
    elif data == "back":
        clear_pending(context)
        await show(M["choose"], main_menu(user.id))
    elif data == "cancel":
        clear_pending(context)
        await show(M["cancelled"], main_menu(user.id))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text sent after pressing Activate or an admin input button."""
    user = update.effective_user
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    ud = context.user_data

    action = ud.get("admin_action") if user.id in ADMIN_IDS else None
    awaiting = ud.get("awaiting_code")
    if not action and not awaiting:
        return  # not waiting for anything: ignore ordinary chat text

    prompt_id = ud.get("prompt_id")

    # keep the chat tidy: remove what the user just typed (code / password / ID)
    try:
        await update.message.delete()
    except Exception:
        pass

    async def show(t, markup=None, mode=None):
        """Show the answer inside the bot's prompt message (edited in place)."""
        return await render(context.bot, chat_id, prompt_id, t, markup, mode)

    # ---- admin inputs
    if action:
        if action in ("acc_username", "acc_password"):
            if not text:
                ud["prompt_id"] = await show(
                    M["acc_empty_input"] + fail_mark(context), cancel_menu()
                )
                return
            if len(text) > MAX_FIELD_LEN:
                ud["prompt_id"] = await show(
                    M["acc_too_long"] + fail_mark(context), cancel_menu()
                )
                return

            if action == "acc_username":
                ud.pop("tries", None)
                ud["new_acc_username"] = text
                ud["admin_action"] = "acc_password"
                ud["prompt_id"] = await show(M["ask_acc_password"], cancel_menu())
                return

            # action == "acc_password"
            username = ud.get("new_acc_username")
            clear_pending(context)
            if not username:
                await show(M["cancelled"], admin_menu())
                return
            acc_id = await asyncio.to_thread(db_add_account, username, text)
            await show_account(
                context.bot, chat_id, prompt_id, acc_id, prefix=M["acc_created"]
            )
            return

        if action == "check_user":
            row = await asyncio.to_thread(db_find_user, text)
            if not row:
                ud["prompt_id"] = await show(
                    M["user_not_found"] + fail_mark(context), cancel_menu()
                )
                return
            uid, uname, expiry = row
            if expiry > datetime.now(timezone.utc):
                state = M["user_state_active"].format(
                    date=fmt_date(expiry), left=fmt_remaining(expiry)
                )
            else:
                state = M["user_state_expired"].format(date=fmt_date(expiry))
            clear_pending(context)
            await show(
                M["user_info"].format(
                    id=uid, name=("@" + uname) if uname else "-", state=state
                ),
                admin_menu(),
            )
        elif action == "revoke":
            ok = await asyncio.to_thread(db_revoke, text.upper())
            clear_pending(context)
            await show(M["revoked"] if ok else M["revoke_fail"], admin_menu())
        return

    # ---- activation code
    message, ok = await do_redeem(text.upper(), user)
    if ok:
        clear_pending(context)
        await show(message, main_menu(user.id), ParseMode.HTML)
    else:
        ud["prompt_id"] = await show(
            message + fail_mark(context) + M["retry_code"], cancel_menu()
        )


# -------------------------------------------------------- admin commands
@admin_only
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await admin_panel_text(), reply_markup=admin_menu())


@admin_only
async def gen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/gen [days] [count] - plain codes with no username/password attached."""
    try:
        days = int(context.args[0]) if len(context.args) > 0 else DEFAULT_DAYS
        count = int(context.args[1]) if len(context.args) > 1 else 1
    except ValueError:
        await update.message.reply_text(M["gen_usage"])
        return
    if not (1 <= days <= 3650 and 1 <= count <= 20):
        await update.message.reply_text(M["gen_range"])
        return
    codes = await asyncio.to_thread(db_generate, update.effective_user.id, days, count)
    body = "\n".join(f"<code>{c}</code>" for c in codes)
    await update.message.reply_text(
        M["gen_many"].format(count=count, days=days, body=body),
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def codes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db_unused_codes)
    if not rows:
        await update.message.reply_text(M["no_codes"])
        return
    body = "\n".join(
        f"<code>{c}</code> ({d} ថ្ងៃ{' · ' + esc(a) if a else ''})" for c, d, a in rows
    )
    await update.message.reply_text(
        M["codes_list"].format(n=len(rows), body=body), parse_mode=ParseMode.HTML
    )


@admin_only
async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(M["revoke_usage"])
        return
    ok = await asyncio.to_thread(db_revoke, context.args[0].strip().upper())
    await update.message.reply_text(M["revoked"] if ok else M["revoke_fail"])


@admin_only
async def user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/user 123456789 - check someone's expiry"""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(M["user_usage"])
        return
    expiry = await asyncio.to_thread(db_get_expiry, int(context.args[0]))
    if not expiry:
        await update.message.reply_text(M["user_never"])
        return
    await update.message.reply_text(
        M["user_cmd_info"].format(date=fmt_date(expiry), left=fmt_remaining(expiry))
    )


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)


# ------------------------------------------------------------------- main
if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("redeem", redeem))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("vip", vip))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("gen", gen))
    app.add_handler(CommandHandler("codes", codes_cmd))
    app.add_handler(CommandHandler("revoke", revoke))
    app.add_handler(CommandHandler("user", user_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.run_polling()