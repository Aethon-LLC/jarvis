import os
import threading
import logging
import requests as http_requests
from flask import Flask, request, jsonify
import anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

flask_app = Flask(__name__)


def send_telegram(text: str) -> None:
    """Send message to David's Telegram via Bot API directly (no async needed)."""
    try:
        http_requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")


@flask_app.route("/webhook", methods=["POST"])
def tradingview_webhook():
    """Receive TradingView alerts → forward to Telegram instantly."""
    data = request.get_json(silent=True) or {}
    alert = data.get("text") or data.get("message") or request.get_data(as_text=True)

    if not alert:
        return jsonify({"error": "empty alert"}), 400

    send_telegram(f"🚨 *Alert*\n\n{alert}")
    logger.info(f"Alert forwarded: {alert[:80]}")
    return jsonify({"status": "ok"}), 200


@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "jarvis-v1"}), 200


def run_flask() -> None:
    flask_app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *Jarvis online.*\n\n"
        "TradingView alerts → Telegram ✓\n"
        "Ask me anything about your trades.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route text messages to Claude Haiku."""
    user_message = update.message.text
    await update.message.reply_chat_action("typing")

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=(
            "You are Jarvis, David Fischbarg's personal AI trading assistant on Telegram. "
            "David trades MES futures on a TPT 150K prop firm evaluation. "
            "Risk per trade: $400. TP1: 2R (50% off, stop to BE). TP2: 3R. Daily stop: -$800. "
            "Answer concisely and directly. No disclaimers. No fluff."
        ),
        messages=[{"role": "user", "content": user_message}],
    )

    await update.message.reply_text(response.content[0].text)


def main() -> None:
    # Start Flask webhook server in background
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Webhook server running on port 8080")

    # Start Telegram bot (polling — blocking)
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Jarvis polling for messages...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
