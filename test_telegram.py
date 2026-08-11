"""Manual Telegram delivery test. Safe to import during test discovery."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    load_dotenv(ROOT / ".env", override=True)

    from src.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    from src.notifier import configured_channels, send_telegram

    print("=" * 50)
    print("SkyWatch Telegram manual test")
    print("=" * 50)
    print(f"Bot token present: {'YES' if TELEGRAM_BOT_TOKEN else 'NO'}")
    print(f"Chat ID present:   {'YES' if TELEGRAM_CHAT_ID else 'NO'}")
    print(f"Configured channels: {configured_channels()}")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is empty in .env")
        return 1

    confirmation = input("This will send one Telegram message. Type SEND to continue: ").strip()
    if confirmation != "SEND":
        print("Canceled; no message was sent.")
        return 0

    ok, detail = send_telegram(
        "[SkyWatch] Telegram manual test\n"
        f"Time: {datetime.now().isoformat(timespec='seconds')}\n"
        "Notification delivery is working."
    )
    print(f"Result: {'SUCCESS' if ok else 'FAILED'} ({detail})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
