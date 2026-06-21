#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
텔레그램 명령어 체크 — "renew" 메시지를 감지하면 모니터를 실행.
5분마다 GitHub Actions로 실행됨.
"""

import os
import json
import requests

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
OFFSET_FILE = "tg_offset.json"


def load_offset() -> int:
    if os.path.exists(OFFSET_FILE):
        with open(OFFSET_FILE) as f:
            return json.load(f).get("offset", 0)
    return 0


def save_offset(offset: int):
    with open(OFFSET_FILE, "w") as f:
        json.dump({"offset": offset}, f)


def check_for_renew() -> bool:
    """텔레그램에서 새 메시지를 확인, 'renew'가 있으면 True."""
    if not TG_TOKEN:
        print("[경고] TELEGRAM_TOKEN 미설정")
        return False

    offset = load_offset()
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 0}

    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
    except Exception as e:
        print(f"[오류] getUpdates 실패: {e}")
        return False

    if not data.get("ok"):
        print(f"[오류] 텔레그램 응답: {data}")
        return False

    found_renew = False
    max_id = offset

    for update in data.get("result", []):
        update_id = update["update_id"]
        if update_id >= max_id:
            max_id = update_id + 1

        msg = update.get("message", {})
        text = msg.get("text", "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        # 본인 chat에서 온 renew 명령만 처리
        if chat_id == TG_CHAT and text in ("renew", "/renew", "다시", "리뉴", "고고", "ㄱㄱ"):            
            found_renew = True
            print(f"[감지] renew 명령어 발견 (update_id={update_id})")

    # offset 저장 (다음에 같은 메시지 재처리 방지)
    if max_id > offset:
        save_offset(max_id)

    return found_renew


if __name__ == "__main__":
    if check_for_renew():
        print("[실행] 모니터 실행...")
        import monitor
        monitor.main()
    else:
        print("[대기] renew 명령 없음 — 스킵")
