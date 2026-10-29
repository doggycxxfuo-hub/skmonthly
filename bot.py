import asyncio
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


def new_code() -> str:
    parts = ["".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(3)]
    return "SK-" + "-".join(parts)


def db_generate(admin_id: int, days: int, count: int) -> list[str]:
    now = datetime.now(timezone.utc)
    codes = [new_code() for _ in range(count)]
    with connect() as conn:
        for c in codes:
            conn.execute(
                "INSERT INTO codes (code, days, created_at, created_by) "
                "VALUES (%s, %s, %s, %s)",
                (c, days, now, admin_id),
            )
    return codes


def db_redeem(code: str, user_id: int, username: str | None):
    """Returns (days, new_expiry) or None if the code is invalid / already used."""
    now = datetime.now(timezone.utc)
    with connect() as conn:
        # atomic: only one person can ever claim a code
        row = conn.execute(
            "UPDATE codes SET redeemed_by = %s, redeemed_at = %s "
            "WHERE code = %s AND redeemed_by IS NULL RETURNING days",
            (user_id, now, code),
        ).fetchone()
        if not row:
            return None
        days = row[0]

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
    return days, new_expiry


def db_get_expiry(user_id: int):
    with connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM users WHERE user_id = %s", (user_id,)
        ).fetchone()
    return row[0] if row else None


def db_unused_codes(limit: int = 50):
    with connect() as conn:
        return conn.execute(
            "SELECT code, days FROM codes WHERE redeemed_by IS NULL "
            "ORDER BY created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()


def db_revoke(code: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM codes WHERE code = %s AND redeemed_by IS NULL", (code,)
        )
        return cur.rowcount > 0



# ======================================================================
#  MESSAGES (Khmer) - edit any text here, the code below doesn't need to change
# ======================================================================
M = {
    # buttons
    "btn_account": "👤 គណនី",
    "btn_activate": "🔑 ធ្វើឱ្យសកម្ម",
    "btn_admin": "🛠 ផ្ទាំងអ្នកគ្រប់គ្រង",
    "btn_cancel": "✖ បោះបង់",
    "btn_back": "⬅ ត្រឡប់ក្រោយ",
    "btn_gen": "➕ {days} ថ្ងៃ",
    "btn_codes": "📋 កូដមិនទាន់ប្រើ",
    "btn_user": "🔍 ពិនិត្យអ្នកប្រើ",
    "btn_revoke": "🗑 លុបកូដ",
    # general
    "welcome": "សូមស្វាគមន៍! សូមជ្រើសរើសជម្រើស៖",
    "choose": "សូមជ្រើសរើសជម្រើស៖",
    "cancelled": "បានបោះបង់។",
    "admins_only": "ពាក្យបញ្ជានេះសម្រាប់តែអ្នកគ្រប់គ្រងប៉ុណ្ណោះ។",
    "myid": "លេខសម្គាល់ Telegram របស់អ្នក: {id}",
    # time
    "expired_word": "ផុតកំណត់ហើយ",
    "remaining_fmt": "{d} ថ្ងៃ {h} ម៉ោង {m} នាទី",
    # account
    "acc_title": "👤 គណនី",
    "acc_username": "ឈ្មោះអ្នកប្រើ: {v}",
    "acc_id": "លេខសម្គាល់អ្នកប្រើ (ID): {v}",
    "acc_not_active": "ស្ថានភាព: មិនទាន់ធ្វើឱ្យសកម្ម",
    "acc_expired": "ស្ថានភាព: ផុតកំណត់",
    "acc_expired_on": "ផុតកំណត់នៅ: {v}",
    "acc_active": "ស្ថានភាព: សកម្ម",
    "acc_expires": "ផុតកំណត់: {v}",
    "acc_remaining": "ពេលវេលានៅសល់: {v}",
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
        "អ្នកប្រើសកម្ម: {active}\n"
        "អ្នកប្រើសរុប: {total}\n"
        "កូដមិនទាន់ប្រើ: {unused}\n\n"
        "សូមជ្រើសរើសសកម្មភាព៖"
    ),
    "gen_one": (
        "កូដថ្មី {days} ថ្ងៃ:\n\n<code>{code}</code>\n\n"
        "ចុចលើកូដដើម្បីចម្លង រួចផ្ញើទៅអ្នកប្រើ។"
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
}


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
    return active, total, unused


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
                InlineKeyboardButton(M["btn_gen"].format(days=d), callback_data=f"adm_gen_{d}")
                for d in (7, 30, 90)
            ],
            [
                InlineKeyboardButton(M["btn_codes"], callback_data="adm_codes"),
                InlineKeyboardButton(M["btn_user"], callback_data="adm_user"),
            ],
            [InlineKeyboardButton(M["btn_revoke"], callback_data="adm_revoke")],
            [InlineKeyboardButton(M["btn_back"], callback_data="back")],
        ]
    )


def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(M["btn_cancel"], callback_data="cancel")]]
    )


def clear_pending(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_code", None)
    context.user_data.pop("admin_action", None)


async def admin_panel_text() -> str:
    active, total, unused = await asyncio.to_thread(db_stats)
    return M["panel"].format(active=active, total=total, unused=unused)


async def account_text(user) -> str:
    expiry = await asyncio.to_thread(db_get_expiry, user.id)
    name = f"@{user.username}" if user.username else (user.full_name or "-")
    lines = [
        M["acc_title"],
        M["acc_username"].format(v=name),
        M["acc_id"].format(v=user.id),
    ]
    if not expiry:
        lines.append(M["acc_not_active"])
    elif expiry <= datetime.now(timezone.utc):
        lines += [M["acc_expired"], M["acc_expired_on"].format(v=fmt_date(expiry))]
    else:
        lines += [
            M["acc_active"],
            M["acc_expires"].format(v=fmt_date(expiry)),
            M["acc_remaining"].format(v=fmt_remaining(expiry)),
        ]
    return "\n".join(lines)


async def do_redeem(code: str, user) -> tuple[str, bool]:
    """Shared by /redeem and the Activate button. Returns (message, success)."""
    result = await asyncio.to_thread(db_redeem, code, user.id, user.username)
    if result is None:
        return M["bad_code"], False
    days, expiry = result
    text = "\n".join(
        [
            M["activated"].format(days=days),
            M["acc_expires"].format(v=fmt_date(expiry)),
            M["acc_remaining"].format(v=fmt_remaining(expiry)),
        ]
    )
    return text, True


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
        message, reply_markup=main_menu(update.effective_user.id)
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

    # ---- admin buttons (always re-check the user is an admin)
    if data == "admin" or data.startswith("adm_"):
        if user.id not in ADMIN_IDS:
            await msg.reply_text(M["admins_only"])
            return
        clear_pending(context)

        if data == "admin":
            await msg.reply_text(await admin_panel_text(), reply_markup=admin_menu())

        elif data.startswith("adm_gen_"):
            days = int(data.rsplit("_", 1)[1])
            codes = await asyncio.to_thread(db_generate, user.id, days, 1)
            await msg.reply_text(
                M["gen_one"].format(days=days, code=codes[0]),
                parse_mode=ParseMode.HTML,
                reply_markup=admin_menu(),
            )

        elif data == "adm_codes":
            rows = await asyncio.to_thread(db_unused_codes)
            if not rows:
                text, mode = M["no_codes"], None
            else:
                body = "\n".join(f"<code>{c}</code> ({d} ថ្ងៃ)" for c, d in rows)
                text = M["codes_list"].format(n=len(rows), body=body)
                mode = ParseMode.HTML
            await msg.reply_text(text, parse_mode=mode, reply_markup=admin_menu())

        elif data == "adm_user":
            context.user_data["admin_action"] = "check_user"
            await msg.reply_text(M["ask_user"], reply_markup=cancel_menu())

        elif data == "adm_revoke":
            context.user_data["admin_action"] = "revoke"
            await msg.reply_text(M["ask_revoke"], reply_markup=cancel_menu())
        return

    # ---- normal user buttons
    if data == "account":
        clear_pending(context)
        await msg.reply_text(await account_text(user), reply_markup=main_menu(user.id))
    elif data == "activate":
        clear_pending(context)
        context.user_data["awaiting_code"] = True
        await msg.reply_text(M["ask_code"], reply_markup=cancel_menu())
    elif data == "back":
        clear_pending(context)
        await msg.reply_text(M["choose"], reply_markup=main_menu(user.id))
    elif data == "cancel":
        clear_pending(context)
        await msg.reply_text(M["cancelled"], reply_markup=main_menu(user.id))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text sent after pressing Activate or an admin input button."""
    user = update.effective_user
    text = update.message.text.strip()

    # ---- admin inputs
    action = context.user_data.get("admin_action")
    if action and user.id in ADMIN_IDS:
        if action == "check_user":
            row = await asyncio.to_thread(db_find_user, text)
            if not row:
                await update.message.reply_text(
                    M["user_not_found"], reply_markup=cancel_menu()
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
            await update.message.reply_text(
                M["user_info"].format(
                    id=uid, name=("@" + uname) if uname else "-", state=state
                ),
                reply_markup=admin_menu(),
            )
        elif action == "revoke":
            ok = await asyncio.to_thread(db_revoke, text.upper())
            clear_pending(context)
            await update.message.reply_text(
                M["revoked"] if ok else M["revoke_fail"], reply_markup=admin_menu()
            )
        return

    # ---- activation code
    if context.user_data.get("awaiting_code"):
        message, ok = await do_redeem(text.upper(), user)
        if ok:
            clear_pending(context)
            await update.message.reply_text(message, reply_markup=main_menu(user.id))
        else:
            await update.message.reply_text(
                message + M["retry_code"], reply_markup=cancel_menu()
            )


# -------------------------------------------------------- admin commands
@admin_only
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await admin_panel_text(), reply_markup=admin_menu())


@admin_only
async def gen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/gen [days] [count]   e.g. /gen   /gen 30 5"""
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
    body = "\n".join(f"<code>{c}</code> ({d} ថ្ងៃ)" for c, d in rows)
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