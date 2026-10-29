import os
import threading
from flask import Flask
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# --- tiny web server so Render sees an open port ---
web = Flask(__name__)

@web.route("/")
def health():
    return "Bot is running", 200

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web.run(host="0.0.0.0", port=port)

# --- bot handlers ---
async def start(update, context):
    await update.message.reply_text("Hello! The bot is working.")

async def echo(update, context):
    await update.message.reply_text(update.message.text)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()

    token = os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.run_polling()