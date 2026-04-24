# Jarvis — CLAUDE.md

Jarvis V1: Python Telegram bot deployed on Hetzner via Coolify.

## What it does
- Receives TradingView webhook alerts → sends to David's Telegram instantly
- Answers David's text/voice questions via Claude Haiku
- Webhook endpoint: POST /webhook (port 8080)
- Health check: GET /health

## Stack
Python 3.12 | python-telegram-bot 21 | Flask | Anthropic SDK | Docker

## Env vars (all in 1Password)
- TELEGRAM_TOKEN — from @BotFather
- TELEGRAM_CHAT_ID — David's personal Telegram chat ID
- ANTHROPIC_API_KEY — claude-haiku-4-5-20251001

## Deployed at
Server: 87.99.129.241
Managed by: Coolify at http://87.99.129.241:8000
Webhook URL: http://87.99.129.241:8080/webhook (use in TradingView alerts)

## V2 upgrades (Sprint 5)
- Connect to Supabase → answer live data questions ("what's my win rate today?")
- Morning brief pulls from live journal data
