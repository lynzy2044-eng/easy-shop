"""แจ้งเตือนเข้า Discord ผ่าน Webhook (ไม่ต้องรันบอท)"""
import os

import requests
from dotenv import load_dotenv

load_dotenv()


def discord_notify(title, description="", color=0x00FF66, fields=None):
    url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not url:
        return
    embed = {
        "title": title,
        "color": color,
        "fields": [
            {"name": str(k), "value": str(v), "inline": True}
            for k, v in (fields or {}).items()
        ],
    }
    if description:
        embed["description"] = description
    try:
        requests.post(url, json={"embeds": [embed]}, timeout=5)
    except requests.RequestException as e:
        print(f"[discord] แจ้งเตือนไม่สำเร็จ: {e}")
