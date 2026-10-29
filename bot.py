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
        return "expired"
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes = rem // 60
    return f"{days}d {hours}h {minutes}m"


def fmt_date(dt: datetime) -> str:
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M (UTC+7)")


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("This command is for admins only.")
            return
        return await func(update, context)

    return wrapper


def active_only(func):
    """Use on any command that should only work for users with a valid code."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        expiry = await asyncio.to_thread(db_get_expiry, update.effective_user.id)
        if not expiry or expiry <= datetime.now(timezone.utc):
            await update.message.reply_text(
                "Your access has expired or is not activated.\n"
                "Use /redeem YOUR-CODE to activate."
            )
            return
        return await func(update, context)

    return wrapper


# ------------------------------------------------------------------ menus
def main_menu(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("👤 Account", callback_data="account"),
            InlineKeyboardButton("🔑 Activate", callback_data="activate"),
        ]
    ]
    if user_id in ADMIN_IDS:
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ 7 days", callback_data="adm_gen_7"),
                InlineKeyboardButton("➕ 30 days", callback_data="adm_gen_30"),
                InlineKeyboardButton("➕ 90 days", callback_data="adm_gen_90"),
            ],
            [
                InlineKeyboardButton("📋 Unused codes", callback_data="adm_codes"),
                InlineKeyboardButton("🔍 Check user", callback_data="adm_user"),
            ],
            [InlineKeyboardButton("🗑 Revoke code", callback_data="adm_revoke")],
            [InlineKeyboardButton("⬅ Back", callback_data="back")],
        ]
    )


def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖ Cancel", callback_data="cancel")]]
    )


def clear_pending(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_code", None)
    context.user_data.pop("admin_action", None)


async def admin_panel_text() -> str:
    active, total, unused = await asyncio.to_thread(db_stats)
    return (
        "🛠 Admin Panel\n"
        f"Active users: {active}\n"
        f"Total users: {total}\n"
        f"Unused codes: {unused}\n\n"
        "Pick an action:"
    )


async def account_text(user) -> str:
    expiry = await asyncio.to_thread(db_get_expiry, user.id)
    name = f"@{user.username}" if user.username else (user.full_name or "-")
    lines = ["👤 Account", f"Username: {name}", f"User ID: {user.id}"]
    if not expiry:
        lines.append("Status: not activated")
    elif expiry <= datetime.now(timezone.utc):
        lines += ["Status: expired", f"Expired on: {fmt_date(expiry)}"]
    else:
        lines += [
            "Status: active",
            f"Expires: {fmt_date(expiry)}",
            f"Remaining: {fmt_remaining(expiry)}",
        ]
    return "\n".join(lines)


async def do_redeem(code: str, user) -> tuple[str, bool]:
    """Shared by /redeem and the Activate button. Returns (message, success)."""
    result = await asyncio.to_thread(db_redeem, code, user.id, user.username)
    if result is None:
        return "Invalid or already used code.", False
    days, expiry = result
    return (
        f"✅ Activated! +{days} days.\n"
        f"Expires: {fmt_date(expiry)}\n"
        f"Remaining: {fmt_remaining(expiry)}",
        True,
    )


# --------------------------------------------------------- user commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_pending(context)
    await update.message.reply_text(
        "Welcome! Choose an option:",
        reply_markup=main_menu(update.effective_user.id),
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram ID: {update.effective_user.id}")


async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /redeem SK-XXXX-XXXX-XXXX")
        return
    code = context.args[0].strip().upper()
    message, _ = await do_redeem(code, update.effective_user)
    await update.message.reply_text(
        message, reply_markup=main_menu(update.effective_user.id)
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expiry = await asyncio.to_thread(db_get_expiry, update.effective_user.id)
    if not expiry:
        await update.message.reply_text("Not activated. Use /redeem YOUR-CODE")
        return
    if expiry <= datetime.now(timezone.utc):
        await update.message.reply_text(
            f"Your access expired on {fmt_date(expiry)}.\n"
            "Ask the admin for a new code, then use /redeem."
        )
        return
    await update.message.reply_text(
        f"Active\nExpires: {fmt_date(expiry)}\nTime left: {fmt_remaining(expiry)}"
    )


@active_only
async def vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # example of a protected command - replace with your real feature
    await update.message.reply_text("You have an active subscription. Welcome in!")


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
            await msg.reply_text("Admins only.")
            return
        clear_pending(context)

        if data == "admin":
            await msg.reply_text(await admin_panel_text(), reply_markup=admin_menu())

        elif data.startswith("adm_gen_"):
            days = int(data.rsplit("_", 1)[1])
            codes = await asyncio.to_thread(db_generate, user.id, days, 1)
            await msg.reply_text(
                f"New {days}-day code:\n\n<code>{codes[0]}</code>\n\n"
                "Tap the code to copy it, then send it to the user.",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_menu(),
            )

        elif data == "adm_codes":
            rows = await asyncio.to_thread(db_unused_codes)
            if not rows:
                text, mode = "No unused codes.", None
            else:
                body = "\n".join(f"<code>{c}</code> ({d}d)" for c, d in rows)
                text, mode = f"Unused codes ({len(rows)}):\n\n{body}", ParseMode.HTML
            await msg.reply_text(text, parse_mode=mode, reply_markup=admin_menu())

        elif data == "adm_user":
            context.user_data["admin_action"] = "check_user"
            await msg.reply_text(
                "Send the user's Telegram ID or @username.",
                reply_markup=cancel_menu(),
            )

        elif data == "adm_revoke":
            context.user_data["admin_action"] = "revoke"
            await msg.reply_text(
                "Send the code you want to revoke (SK-XXXX-XXXX-XXXX).",
                reply_markup=cancel_menu(),
            )
        return

    # ---- normal user buttons
    if data == "account":
        clear_pending(context)
        await msg.reply_text(await account_text(user), reply_markup=main_menu(user.id))
    elif data == "activate":
        clear_pending(context)
        context.user_data["awaiting_code"] = True
        await msg.reply_text(
            "Please send your activation code now.\nExample: SK-XXXX-XXXX-XXXX",
            reply_markup=cancel_menu(),
        )
    elif data == "back":
        clear_pending(context)
        await msg.reply_text("Choose an option:", reply_markup=main_menu(user.id))
    elif data == "cancel":
        clear_pending(context)
        await msg.reply_text("Cancelled.", reply_markup=main_menu(user.id))


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
                    "No user found. Send another ID / @username, or press Cancel.",
                    reply_markup=cancel_menu(),
                )
                return
            uid, uname, expiry = row
            state = (
                f"Active\nExpires: {fmt_date(expiry)}\nRemaining: {fmt_remaining(expiry)}"
                if expiry > datetime.now(timezone.utc)
                else f"Expired on {fmt_date(expiry)}"
            )
            clear_pending(context)
            await update.message.reply_text(
                f"👤 User\nID: {uid}\nUsername: {'@' + uname if uname else '-'}\n{state}",
                reply_markup=admin_menu(),
            )
        elif action == "revoke":
            ok = await asyncio.to_thread(db_revoke, text.upper())
            clear_pending(context)
            await update.message.reply_text(
                "Code revoked." if ok else "Code not found or already used.",
                reply_markup=admin_menu(),
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
                message + "\nSend the code again, or press Cancel.",
                reply_markup=cancel_menu(),
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
        await update.message.reply_text("Usage: /gen [days] [count]")
        return
    if not (1 <= days <= 3650 and 1 <= count <= 20):
        await update.message.reply_text("days must be 1-3650 and count 1-20.")
        return
    codes = await asyncio.to_thread(db_generate, update.effective_user.id, days, count)
    body = "\n".join(f"<code>{c}</code>" for c in codes)
    await update.message.reply_text(
        f"Generated {count} code(s), {days} days each:\n\n{body}\n\n"
        "Tap a code to copy it, then send it to the user.",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def codes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db_unused_codes)
    if not rows:
        await update.message.reply_text("No unused codes.")
        return
    body = "\n".join(f"<code>{c}</code> ({d}d)" for c, d in rows)
    await update.message.reply_text(
        f"Unused codes ({len(rows)}):\n\n{body}", parse_mode=ParseMode.HTML
    )


@admin_only
async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /revoke SK-XXXX-XXXX-XXXX")
        return
    ok = await asyncio.to_thread(db_revoke, context.args[0].strip().upper())
    await update.message.reply_text(
        "Code revoked." if ok else "Code not found or already used."
    )


@admin_only
async def user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/user 123456789 - check someone's expiry"""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /user TELEGRAM_ID")
        return
    expiry = await asyncio.to_thread(db_get_expiry, int(context.args[0]))
    if not expiry:
        await update.message.reply_text("That user has never activated a code.")
        return
    await update.message.reply_text(
        f"Expires: {fmt_date(expiry)}\nTime left: {fmt_remaining(expiry)}"
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