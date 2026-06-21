#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
반도체 사이클 모니터 봇
- SK하이닉스 / 삼성전자 종가를 매일 수집
- 시즌 시작점(2026-03-30) 이후 '구간 고점'을 추적·갱신
- 고점 대비 -15% / -25% 트리거를 '종가 3일 연속' 조건으로 판정
- 주식 내 반도체 비중(상한 70%)도 함께 점검
- 매일 신호등(여유/근접/임박/도달)을 텔레그램으로 전송

설계 원칙: 봇은 '사실(거리)'만 보고한다. 사라/팔라는 예측·조언은 하지 않는다.
트리거에 '도달'했을 때만 정해진 규칙(최초 수량의 1/3 매도)을 알린다.
"""

import os
import json
import datetime
import requests

# ──────────────────────────────────────────────────────────────
# 설정 (여기만 고치면 됨)
# ──────────────────────────────────────────────────────────────

SEASON_START = "2026-03-30"   # 이번 시즌 시작점 — 이 날짜 이후 종가로 고점 추적

# 추적 종목: 코드, 이름, 최초 보유수량(트리거 매도단위 = 최초의 1/3 계산용)
STOCKS = {
    "000660": {"name": "SK하이닉스", "init_shares": 240},   # 76+73+40+18+17+16 = 240
    "005930": {"name": "삼성전자",   "init_shares": 1237},  # 320+303+172+151+146+143+2 = 1237
}

# 트리거 단계: (고점대비 하락률, 매도 라벨). 코어 1/3 보존 → 2단계까지만.
TRIGGERS = [
    {"level": 0.15, "label": "1차", "sell": "최초 수량의 1/3"},
    {"level": 0.25, "label": "2차", "sell": "최초 수량의 1/3 (누적 2/3, 코어 1/3 보존)"},
]

CONFIRM_DAYS = 3          # 종가가 트리거선 이하로 며칠 연속이어야 '도달'로 인정
SEMI_TARGET_RATIO = 0.70  # 주식 내 반도체 비중 상한

# 신호등 임계 (트리거선까지 남은 거리, %p 기준)
NEAR = 5.0   # 🟡 근접
IMMINENT = 2.0  # 🟠 임박

STATE_FILE = "state.json"

# 시크릿 (GitHub Actions Secrets로 주입)
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


# ──────────────────────────────────────────────────────────────
# 가격 수집 (네이버 금융)
# ──────────────────────────────────────────────────────────────

def fetch_close(code: str) -> float:
    """네이버 금융 모바일 통합 API에서 현재가(종가) 가져오기."""
    url = f"https://m.stock.naver.com/api/stock/{code}/integration"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()
    # closePrice는 "138,500" 형태 문자열
    price_str = data["dealTrendInfos"][0]["closePrice"] if "dealTrendInfos" in data else None
    if price_str is None:
        # 폴백: totalInfos에서 현재가 탐색
        for item in data.get("totalInfos", []):
            if item.get("code") in ("nv", "cv", "현재가"):
                price_str = item.get("value")
                break
    return float(price_str.replace(",", ""))


def fetch_semi_value_ratio() -> float | None:
    """
    주식 내 반도체 비중. 자동화하려면 구글시트 등 외부 소스 연동이 필요.
    여기서는 None을 반환하고, 연동 전까지는 수동 입력값(아래 MANUAL)을 사용.
    """
    return None


MANUAL_SEMI_RATIO = 0.62  # 시트 연동 전까지 수동값 (현재 62%)


# ──────────────────────────────────────────────────────────────
# 상태 저장/로드 (고점 + 트리거선 이하 연속일 카운트)
# ──────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    # 초기 상태
    return {
        "season_start": SEASON_START,
        "stocks": {code: {"peak": 0.0, "streak": {t["label"]: 0 for t in TRIGGERS},
                          "fired": {t["label"]: False for t in TRIGGERS}}
                   for code in STOCKS},
    }


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────────────────────
# 핵심 로직
# ──────────────────────────────────────────────────────────────

def evaluate(code: str, close: float, st: dict) -> dict:
    """한 종목에 대해 고점 갱신·트리거 판정·신호등 산출."""
    name = STOCKS[code]["name"]
    init_shares = STOCKS[code]["init_shares"]

    # 고점 갱신
    if close > st["peak"]:
        st["peak"] = close
    peak = st["peak"]

    drop = (peak - close) / peak if peak > 0 else 0.0  # 고점 대비 하락률
    drop_pct = drop * 100

    # 가장 가까운(아직 안 터진) 트리거선까지의 거리
    next_trigger = None
    for t in TRIGGERS:
        if not st["fired"][t["label"]]:
            next_trigger = t
            break

    lines = []
    status = "🟢 여유"
    distance_pp = None

    if next_trigger:
        line_pct = next_trigger["level"] * 100
        distance_pp = line_pct - drop_pct  # 트리거선까지 남은 %p
        if distance_pp <= 0:
            status = "🔴 도달구간"
        elif distance_pp <= IMMINENT:
            status = "🟠 임박"
        elif distance_pp <= NEAR:
            status = "🟡 근접"
        else:
            status = "🟢 여유"
    else:
        status = "✅ 트리거 모두 소진 (코어 1/3 보존 중)"

    # 트리거별 연속일 카운트 & 발동 판정
    fired_now = []
    for t in TRIGGERS:
        label = t["label"]
        if st["fired"][label]:
            continue
        if drop >= t["level"]:
            st["streak"][label] += 1
            if st["streak"][label] >= CONFIRM_DAYS:
                st["fired"][label] = True
                sell_units = init_shares // 3
                fired_now.append({
                    "label": label,
                    "level_pct": t["level"] * 100,
                    "sell": t["sell"],
                    "sell_shares": sell_units,
                })
        else:
            st["streak"][label] = 0  # 연속 끊김

    return {
        "name": name, "close": close, "peak": peak,
        "drop_pct": drop_pct, "status": status,
        "distance_pp": distance_pp, "next_trigger": next_trigger,
        "fired_now": fired_now,
        "streak": dict(st["streak"]),
    }


def build_message(results: list, semi_ratio: float) -> str:
    today = datetime.date.today().isoformat()
    lines = [f"📊 *반도체 모니터*  {today}", ""]

    any_fired = False
    for r in results:
        lines.append(f"*{r['name']}*")
        lines.append(f"종가 {r['close']:,.0f}원 / 구간고점 {r['peak']:,.0f}원")
        lines.append(f"고점 대비 *-{r['drop_pct']:.1f}%*")
        if r["next_trigger"] and r["distance_pp"] is not None:
            nt = r["next_trigger"]
            if r["distance_pp"] > 0:
                lines.append(f"→ {nt['label']}선(-{nt['level']*100:.0f}%)까지 여유 {r['distance_pp']:.1f}%p")
            else:
                streak = r["streak"].get(nt["label"], 0)
                lines.append(f"→ {nt['label']}선 진입! 3일 카운트 {streak}/{CONFIRM_DAYS}일째")
        lines.append(f"상태: {r['status']}")
        if r["fired_now"]:
            any_fired = True
            for f in r["fired_now"]:
                lines.append(f"🚨 *{f['label']} 트리거 발동 (-{f['level_pct']:.0f}%, 3일 확인 완료)*")
                lines.append(f"   → {f['sell']} = 약 {f['sell_shares']}주 매도")
                lines.append(f"   → 매도 순서: 일반 과세계좌 먼저, 연금·ISA 후순위")
        lines.append("")

    # 반도체 비중 (상승 측)
    lines.append(f"*반도체 비중*: 주식의 {semi_ratio*100:.0f}% (상한 {SEMI_TARGET_RATIO*100:.0f}%)")
    if semi_ratio > SEMI_TARGET_RATIO:
        over = (semi_ratio - SEMI_TARGET_RATIO) * 100
        lines.append(f"🚨 상한 초과 {over:.1f}%p → 초과분 분할매도 검토")
    else:
        room = (SEMI_TARGET_RATIO - semi_ratio) * 100
        lines.append(f"→ 상한까지 {room:.1f}%p 여유")
    lines.append("")

    if not any_fired and semi_ratio <= SEMI_TARGET_RATIO:
        lines.append("📌 트리거 전 — *행동 없음*. 숫자만 확인.")

    return "\n".join(lines)


def send_telegram(text: str):
    if not TG_TOKEN or not TG_CHAT:
        print("[경고] 텔레그램 시크릿 미설정 — 콘솔 출력만 함")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(url, data={
        "chat_id": TG_CHAT, "text": text, "parse_mode": "Markdown"
    }, timeout=10)
    if r.status_code != 200:
        print("[오류] 텔레그램 전송 실패:", r.text)
    else:
        print("[성공] 텔레그램 전송 완료")


def main():
    state = load_state()
    # 시즌 시작점이 바뀌면 고점 초기화
    if state.get("season_start") != SEASON_START:
        state = {"season_start": SEASON_START,
                 "stocks": {code: {"peak": 0.0,
                                   "streak": {t["label"]: 0 for t in TRIGGERS},
                                   "fired": {t["label"]: False for t in TRIGGERS}}
                            for code in STOCKS}}

    results = []
    for code in STOCKS:
        try:
            close = fetch_close(code)
        except Exception as e:
            print(f"[오류] {code} 가격 수집 실패: {e}")
            continue
        st = state["stocks"].setdefault(code, {
            "peak": 0.0,
            "streak": {t["label"]: 0 for t in TRIGGERS},
            "fired": {t["label"]: False for t in TRIGGERS},
        })
        results.append(evaluate(code, close, st))

    semi_ratio = fetch_semi_value_ratio() or MANUAL_SEMI_RATIO

    if results:
        msg = build_message(results, semi_ratio)
        send_telegram(msg)
        save_state(state)
    else:
        print("[오류] 수집된 가격이 없어 전송 생략")


if __name__ == "__main__":
    main()
