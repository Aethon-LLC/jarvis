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

# Startup diagnostics — print whether the voice-trade env vars are actually
# loaded so we can tell from the Coolify logs whether the issue is "var
# missing" vs "var present but POST failing." Boolean-only — never log the
# secret value itself.
logger.info(
    "Startup env check — "
    f"JARVIS_WEBHOOK_SECRET set: {bool(JARVIS_WEBHOOK_SECRET)} · "
    f"JARVIS_CLERK_USER_ID set: {bool(JARVIS_CLERK_USER_ID)} · "
    f"TRADING_OS_API: {TRADING_OS_API}"
)

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

flask_app = Flask(__name__)

SYSTEM_PROMPT = (
    "You are Jarvis, the user's personal AI trading assistant on Telegram. "
    "Answer concisely and directly. No disclaimers. No fluff. "
    "If the user asks about their stats / strategy / eval status, tell them "
    "to check their Trading OS dashboard at usetradingos.com — you don't "
    "have live read access to their data yet (V1.1). For trade logging, "
    "they can speak or type a trade and you'll log it for them."
    # V1.1: replace with dynamic per-user prompt pulled from
    # /api/jarvis/context (returns user's app_mode, primary_asset_class,
    # active eval rules, risk_per_trade, top strategies, etc.)
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
        # Don't blindly resp.json() — if Vercel returns an HTML error page
        # (auth redirect, 404, 5xx), json() raises a cryptic "Expecting
        # value: line 1 column 1" that hides the real cause. Detect HTML and
        # surface the actual status to Telegram + the logs.
        ct = resp.headers.get("content-type", "")
        if "application/json" not in ct.lower():
            preview = resp.text[:120].replace("\n", " ")
            logger.error(
                f"log-trade non-JSON response: status={resp.status_code} "
                f"content-type={ct!r} body[:120]={preview!r}"
            )
            return {
                "ok": False,
                "message": (
                    f"Trading OS returned {resp.status_code} (not JSON). "
                    "Endpoint may be unauthenticated or undeployed."
                ),
            }
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
        "Text or voice — ask me anything.\n"
        "Send /help to see how to log trades by voice or text.",
        parse_mode="Markdown",
    )


# Voice/text trade-log quick reference. Plain text (no Markdown) so a
# transcription with _ or * in a strategy name doesn't 400 the help reply.
HELP_TEXT = (
    "🎙 Voice + text trade logging\n"
    "\n"
    "Minimum to log a trade:\n"
    "  instrument + direction + (entry OR stop)\n"
    "  → e.g. 'MES long entry 5104 stop 5101'\n"
    "\n"
    "Add detail when you have it (any order, any wording):\n"
    "  exit / target → 'out at 5108' or 'hit TP1' or 'stopped out'\n"
    "  contracts    → '2 contracts' or 'one micro'\n"
    "  conviction   → 'high conviction' / '5 stars' / 'shaky'\n"
    "  mode         → 'eval account' / 'live' / 'sim' / 'replay'\n"
    "  strategy     → 'on my CRT strategy' / 'ran my VWAP setup'\n"
    "  confluences  → 'with FVG and VWAP bounce' / 'had a sweep'\n"
    "  mistake      → 'mistake was early entry' (any losing trade)\n"
    "  notes        → anything else you say lands in notes\n"
    "\n"
    "Strategy vs confluence — important:\n"
    "  STRATEGY  = your full setup. 'on my X strategy' / 'ran X setup'\n"
    "  CONFLUENCE = trigger or factor. 'with X' / 'had X lined up'\n"
    "  Just saying the name (like 'sweep FVG') is read as CONFLUENCE.\n"
    "  Same name in both lists? Frame it: 'on my Sweep FVG strategy'\n"
    "  attaches the strategy; 'with sweep FVG' attaches the filter.\n"
    "\n"
    "Examples that all work:\n"
    "  'MES long 5104 stop 5101 hit TP1'\n"
    "  'eval, MES short at 5118 stop 5121 took the loss, late entry'\n"
    "  'long MGC 2425.5 stop 2424 hit TP2, sweep FVG, conviction 4'\n"
    "  'discretionary scalp NQ short 21450 stop 21465 out at 21430'\n"
    "  'scaled at first then runner went BE, MES long 5104 stop 5101'\n"
    "\n"
    "If you only say 'hit TP1' (no exit price), R falls back to your\n"
    "default targets (TP1=2R, TP2=3R). Per-user custom defaults coming\n"
    "in V1.1 settings.\n"
    "\n"
    "Force trade logging (skip detection): prefix 'log trade' or '/log'\n"
    "Plain Q&A: anything that isn't a trade — Jarvis just chats back."
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


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
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Jarvis polling for messages...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
