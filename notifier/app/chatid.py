"""Print the chat IDs that have messaged the bot, so you can set TELEGRAM_CHAT_ID.

Usage: message your bot once in Telegram, then
    docker compose run --rm notifier python -m app.chatid
"""
import os
import sys

import httpx


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN is empty. Put it in .env first.")
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
    except httpx.HTTPError as e:
        sys.exit(f"Request failed: {type(e).__name__}")
    if r.status_code != 200:
        sys.exit(f"Telegram returned HTTP {r.status_code}. Check the token.")
    seen = {}
    for u in r.json().get("result", []):
        chat = (u.get("message") or u.get("my_chat_member") or {}).get("chat")
        if chat:
            seen[chat["id"]] = chat.get("title") or chat.get("first_name") or chat.get("username") or "?"
    if not seen:
        sys.exit("No messages found. Send any message to your bot in Telegram, then run this again.")
    for cid, label in seen.items():
        print(f"TELEGRAM_CHAT_ID={cid}   # {label}")


if __name__ == "__main__":
    main()
