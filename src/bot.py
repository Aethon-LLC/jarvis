import os
import threading
import logging
import tempfile
import requests as http_requests
from flask import Flask, request, jsonify
import anthropic
from openai import OpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# Voice-trade-logging integration with Trading OS
# (Sprint 5 D7 — May 6 2026). Both env vars set on Coolify side; the
# webhook secret must match the value set on Vercel for /api/voice/log-trade.
JARVIS_WEBHOOK_SECRET = os.environ.get("JARVIS_WEBHOOK_SECRET", "")
JARVIS_CLERK_USER_ID = os.environ.get("JARVIS_CLERK_USER_ID", "")
TRADING_OS_API = os.environ.get(
    "TRADING_OS_API", "https://usetradingos.com/api/voice/log-trade"
)

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

flask_app = Flask(__name__)

SYSTEM_PROMPT = (
    "You are Jarvis, David Fischbarg's personal AI trading assistant on Telegram. "
    "David trades MES futures on a TPT 150K prop firm evaluation. "
    "Risk per trade: $400. TP1: 2R (50% off, stop to BE). TP2: 3R. Daily stop: -$800. "
    "Answer concisely and directly. No disclaimers. No fluff."
)


def send_telegram(text: str) -> None:
    try:
        http_requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")


def ask_claude(text: str) -> str:
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    return response.content[0].text


# Trade-logging keyword detector. Heuristic — if 2+ trade-language tokens
# are present, we route to /api/voice/log-trade. Otherwise plain Q&A.
# Misses are fine: user can prefix with "log trade" to force the route.
_TRADE_KEYWORDS = {
    "long", "short", "entry", "in at", "out at", "stop", "stopped",
    "tp1", "tp2", "tp 1", "tp 2", "target", "took profit", "taking profit",
    "trade", "trailed", "breakeven", "be ", "took the loss", "scaled",
    "contracts", "contract", "mes", "es", "nq", "mnq", "gold", "silver",
    "mgc", "gc", "sil", "si", "long position", "short position",
}


def looks_like_trade(text: str) -> bool:
    if not text:
        return False
    lowered = " " + text.lower() + " "
    if lowered.startswith(" /log") or " log trade " in lowered:
        return True
    hits = sum(1 for kw in _TRADE_KEYWORDS if kw in lowered)
    return hits >= 2


def post_trade_to_os(transcription: str, message_id: int | None = None) -> dict:
    """POST a transcription to Trading OS /api/voice/log-trade.

    Returns the response dict. On HTTP error or missing config, returns
    {"ok": False, "message": "..."} so the caller can render to Telegram.
    """
    if not JARVIS_WEBHOOK_SECRET or not JARVIS_CLERK_USER_ID:
        return {
            "ok": False,
            "message": (
                "Voice trade logging not configured yet. Add "
                "JARVIS_WEBHOOK_SECRET + JARVIS_CLERK_USER_ID to Coolify."
            ),
        }
    try:
        resp = http_requests.post(
            TRADING_OS_API,
            json={
                "secret": JARVIS_WEBHOOK_SECRET,
                "user_id": JARVIS_CLERK_USER_ID,
                "transcription": transcription,
                "telegram_message_id": message_id,
            },
            timeout=30,
        )
        return resp.json()
    except Exception as e:
        logger.error(f"log-trade POST failed: {e}")
        return {"ok": False, "message": f"Couldn't reach Trading OS: {e}"}


def format_trade_reply(api_result: dict, transcription: str) -> str:
    """Build Telegram reply from /api/voice/log-trade response.
    NO Markdown — the original implementation used `_..._` italics around
    the transcription which silently fails if the transcription contains
    underscores or asterisks (Telegram returns 400 + bot reply never
    posts). Plain text always works."""
    if api_result.get("ok"):
        summary = api_result.get("summary", "Trade logged.")
        url = api_result.get("detail_url", "")
        return f"🎙 {transcription}\n\n{summary}\n{url}".strip()
    msg = api_result.get("message", "Couldn't log the trade.")
    return f"🎙 {transcription}\n\n{msg}"


@flask_app.route("/webhook", methods=["POST"])
def tradingview_webhook():
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
        "Text or voice — ask me anything.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Text message handler. Routes to trade-log if the text looks like
    a trade, otherwise falls back to plain Claude Q&A."""
    text = update.message.text or ""
    chat_id = update.effective_chat.id
    is_trade = looks_like_trade(text)
    logger.info(
        f"[text] chat={chat_id} is_trade={is_trade} "
        f"text={text[:80]!r}"
    )

    try:
        await update.message.reply_chat_action("typing")
    except Exception as e:
        logger.warning(f"reply_chat_action failed: {e}")

    if is_trade:
        # Strip leading /log if present
        clean = text
        if clean.lower().startswith("/log"):
            clean = clean[4:].strip()
        logger.info(f"[text] routing to trade-log: {clean[:80]!r}")
        result = post_trade_to_os(clean, message_id=update.message.message_id)
        logger.info(f"[text] trade-log result: ok={result.get('ok')} msg={result.get('message')!r}")
        try:
            await update.message.reply_text(format_trade_reply(result, clean))
        except Exception as e:
            logger.error(f"trade reply failed: {e}")
            await update.message.reply_text(
                f"Trade logged status: {result.get('message', 'unknown')}"
            )
        return

    try:
        reply = ask_claude(text)
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"claude reply failed: {e}")
        await update.message.reply_text(f"Claude error: {e}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voice message handler. Whisper transcribes, then we route the same
    way as text — looks-like-a-trade → log it; else → Claude Q&A."""
    chat_id = update.effective_chat.id
    logger.info(f"[voice] chat={chat_id} received voice note")

    try:
        await update.message.reply_chat_action("typing")
    except Exception as e:
        logger.warning(f"reply_chat_action failed: {e}")

    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await tg_file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as audio:
            transcription = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio,
            )
        text = transcription.text
        is_trade = looks_like_trade(text)
        logger.info(f"[voice] transcribed is_trade={is_trade} text={text[:80]!r}")

        if is_trade:
            result = post_trade_to_os(text, message_id=update.message.message_id)
            logger.info(f"[voice] trade-log result: ok={result.get('ok')} msg={result.get('message')!r}")
            try:
                await update.message.reply_text(format_trade_reply(result, text))
            except Exception as e:
                logger.error(f"voice trade reply failed: {e}")
                await update.message.reply_text(
                    f"Trade logged status: {result.get('message', 'unknown')}"
                )
            return

        reply = ask_claude(text)
        # Plain text — Markdown can fail silently if transcription has _ or *
        await update.message.reply_text(f"🎙 {text}\n\n{reply}")
    except Exception as e:
        logger.error(f"voice handler failed: {e}")
        try:
            await update.message.reply_text(f"Voice error: {e}")
        except Exception:
            pass
    finally:
        os.unlink(tmp_path)


def main() -> None:
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Webhook server running on port 8080")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Jarvis polling for messages...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
