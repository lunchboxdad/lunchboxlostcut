#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
반도체 사이클 모니터 봇 v2
- 1단계: 전체 주식 포트폴리오 평가금액의 구간 고점 대비 하락률 감시
- 2단계: SK하이닉스·삼성전자 개별 종목 고점 대비 하락률 감시
- 코어 1/3 보존형, 3일 연속 확인, 매일 신호등 보고

설계 원칙: 예측·조언 없이 사실(거리)만 보고. 트리거 전엔 행동 없음.
"""

import os
import json
import datetime
import requests

# ──────────────────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────────────────

SEASON_START = "2026-03-30"

# ── 전체 포트폴리오 구성 ──
# 한국 주식 (네이버 금융)
KR_POSITIONS = {
    "000660": {"name": "SK하이닉스",       "shares": 240},
    "005930": {"name": "삼성전자",         "shares": 1237},
    "069500": {"name": "KODEX 200",       "shares": 1504},   # 1077+191+175+60+1
    "292340": {"name": "KODEX 채권혼합50", "shares": 1394},   # 728+666
}

# 미국 주식 (Yahoo Finance, USD 기준 → 환율 적용)
US_POSITIONS = {
    "VOO":  {"name": "S&P500 VOO", "shares": 386},   # 288+98
    "QQQM": {"name": "QQQM",      "shares": 269},
}

# ── 개별 종목 트리거 대상 (반도체만) ──
SEMI_CODES = ["000660", "005930"]  # 개별 트리거는 이 종목에만 적용

# ── 트리거 규칙 ──
TRIGGERS = [
    {"level": 0.15, "label": "1차", "sell": "최초 수량의 1/3"},
    {"level": 0.25, "label": "2차", "sell": "최초 수량의 1/3 (누적 2/3, 코어 1/3 보존)"},
]
CONFIRM_DAYS = 3

# ── 비중 상한 ──
SEMI_TARGET_RATIO = 0.70

# ── 신호등 임계 ──
NEAR = 5.0
IMMINENT = 2.0

STATE_FILE = "state.json"
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


# ──────────────────────────────────────────────────────────────
# 가격 수집
# ──────────────────────────────────────────────────────────────

def fetch_kr_price(code: str) -> float:
    """네이버 금융에서 한국 주식 현재가."""
    url = f"https://m.stock.naver.com/api/stock/{code}/integration"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()

    # dealTrendInfos에서 closePrice 시도
    if "dealTrendInfos" in data and data["dealTrendInfos"]:
        price_str = data["dealTrendInfos"][0].get("closePrice")
        if price_str:
            return float(price_str.replace(",", ""))

    # totalInfos에서 현재가 폴백
    for item in data.get("totalInfos", []):
        if item.get("code") in ("cv", "nv"):
            return float(item["value"].replace(",", ""))

    raise ValueError(f"한국주식 {code} 가격 파싱 실패")


def fetch_us_price(ticker: str) -> float:
    """Yahoo Finance에서 미국 주식 현재가 (USD)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"range": "1d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    meta = data["chart"]["result"][0]["meta"]
    return meta["regularMarketPrice"]


def fetch_usdkrw() -> float:
    """USD/KRW 환율."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/USDKRW=X"
    params = {"range": "1d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data["chart"]["result"][0]["meta"]["regularMarketPrice"]


# ──────────────────────────────────────────────────────────────
# 상태 관리
# ──────────────────────────────────────────────────────────────

def empty_trigger_state():
    return {"streak": {t["label"]: 0 for t in TRIGGERS},
            "fired": {t["label"]: False for t in TRIGGERS}}


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    state = {
        "season_start": SEASON_START,
        "portfolio": {"peak": 0.0, **empty_trigger_state()},
        "stocks": {},
    }
    for code in SEMI_CODES:
        state["stocks"][code] = {"peak": 0.0, **empty_trigger_state()}
    return state


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────────────────────
# 포트폴리오 평가
# ──────────────────────────────────────────────────────────────

def calc_portfolio(kr_prices: dict, us_prices: dict, usdkrw: float) -> dict:
    """전체 주식 포트폴리오 평가금액 + 반도체 비중 계산."""
    total = 0.0
    semi_total = 0.0
    details = {}

    for code, pos in KR_POSITIONS.items():
        if code in kr_prices:
            val = kr_prices[code] * pos["shares"]
            total += val
            details[code] = {"name": pos["name"], "value": val, "price": kr_prices[code]}
            if code in SEMI_CODES:
                semi_total += val
            # KODEX 200 내 반도체 간접 노출 (~30%)
            if code == "069500":
                semi_total += val * 0.30

    for ticker, pos in US_POSITIONS.items():
        if ticker in us_prices:
            val = us_prices[ticker] * usdkrw * pos["shares"]
            total += val
            details[ticker] = {"name": pos["name"], "value": val, "price": us_prices[ticker]}

    semi_ratio = semi_total / total if total > 0 else 0.0
    return {"total": total, "semi_total": semi_total, "semi_ratio": semi_ratio, "details": details}


# ──────────────────────────────────────────────────────────────
# 트리거 판정 (공통 로직)
# ──────────────────────────────────────────────────────────────

def evaluate_triggers(current: float, state: dict, label_prefix: str = "") -> dict:
    """고점 갱신 + 트리거 판정 + 신호등."""
    if current > state["peak"]:
        state["peak"] = current
    peak = state["peak"]

    drop = (peak - current) / peak if peak > 0 else 0.0
    drop_pct = drop * 100

    # 다음 미발동 트리거 찾기
    next_trigger = None
    for t in TRIGGERS:
        if not state["fired"][t["label"]]:
            next_trigger = t
            break

    distance_pp = None
    if next_trigger:
        distance_pp = next_trigger["level"] * 100 - drop_pct
        if distance_pp <= 0:
            status = "🔴 도달구간"
        elif distance_pp <= IMMINENT:
            status = "🟠 임박"
        elif distance_pp <= NEAR:
            status = "🟡 근접"
        else:
            status = "🟢 여유"
    else:
        status = "✅ 트리거 모두 소진 (코어 보존 중)"

    # 연속일 카운트
    fired_now = []
    for t in TRIGGERS:
        label = t["label"]
        if state["fired"][label]:
            continue
        if drop >= t["level"]:
            state["streak"][label] += 1
            if state["streak"][label] >= CONFIRM_DAYS:
                state["fired"][label] = True
                fired_now.append(t)
        else:
            state["streak"][label] = 0

    return {
        "current": current, "peak": peak, "drop_pct": drop_pct,
        "status": status, "distance_pp": distance_pp,
        "next_trigger": next_trigger, "fired_now": fired_now,
        "streak": dict(state["streak"]),
    }


# ──────────────────────────────────────────────────────────────
# 메시지 작성
# ──────────────────────────────────────────────────────────────

def format_억(val: float) -> str:
    return f"{val/1e8:.2f}억"


def build_message(port_result: dict, port_eval: dict,
                  stock_evals: dict, semi_ratio: float) -> str:
    today = datetime.date.today().isoformat()
    lines = [f"📊 *반도체 모니터*  {today}", ""]

    # ── 1단계: 전체 포트폴리오 ──
    lines.append("━━ *전체 주식 포트폴리오* ━━")
    lines.append(f"평가금액 {format_억(port_eval['current'])} / 구간고점 {format_억(port_eval['peak'])}")
    lines.append(f"고점 대비 *-{port_eval['drop_pct']:.1f}%*")

    if port_eval["next_trigger"] and port_eval["distance_pp"] is not None:
        nt = port_eval["next_trigger"]
        if port_eval["distance_pp"] > 0:
            lines.append(f"→ {nt['label']}선(-{nt['level']*100:.0f}%)까지 여유 {port_eval['distance_pp']:.1f}%p")
        else:
            streak = port_eval["streak"].get(nt["label"], 0)
            lines.append(f"→ {nt['label']}선 진입! 3일 카운트 {streak}/{CONFIRM_DAYS}일째")

    lines.append(f"상태: {port_eval['status']}")

    if port_eval["fired_now"]:
        for f in port_eval["fired_now"]:
            lines.append(f"🚨 *포트폴리오 {f['label']} 트리거 발동!*")
            lines.append(f"   → {f['sell']}")
            lines.append(f"   → 매도 순서: 일반 과세계좌 먼저")
    lines.append("")

    # ── 반도체 비중 ──
    lines.append(f"*반도체 비중*: 주식의 {semi_ratio*100:.0f}% (상한 {SEMI_TARGET_RATIO*100:.0f}%)")
    if semi_ratio > SEMI_TARGET_RATIO:
        over = (semi_ratio - SEMI_TARGET_RATIO) * 100
        lines.append(f"🚨 상한 초과 {over:.1f}%p → 초과분 분할매도 검토")
    else:
        room = (SEMI_TARGET_RATIO - semi_ratio) * 100
        lines.append(f"→ 상한까지 {room:.1f}%p 여유")
    lines.append("")

    # ── 2단계: 종목별 세부 ──
    lines.append("━━ *종목별 세부* ━━")
    for code in SEMI_CODES:
        if code not in stock_evals:
            continue
        ev = stock_evals[code]
        name = KR_POSITIONS[code]["name"]
        init_shares = KR_POSITIONS[code]["shares"]

        lines.append(f"*{name}*")
        lines.append(f"종가 {ev['current']:,.0f}원 / 고점 {ev['peak']:,.0f}원 / *-{ev['drop_pct']:.1f}%*")

        if ev["next_trigger"] and ev["distance_pp"] is not None:
            nt = ev["next_trigger"]
            if ev["distance_pp"] > 0:
                lines.append(f"→ {nt['label']}선까지 {ev['distance_pp']:.1f}%p 여유")
            else:
                streak = ev["streak"].get(nt["label"], 0)
                lines.append(f"→ {nt['label']}선 진입! {streak}/{CONFIRM_DAYS}일째")

        lines.append(f"상태: {ev['status']}")

        if ev["fired_now"]:
            for f in ev["fired_now"]:
                sell_shares = init_shares // 3
                lines.append(f"🚨 *{name} {f['label']} 트리거 발동!*")
                lines.append(f"   → {f['sell']} = 약 {sell_shares}주")
        lines.append("")

    # ── 구성 요약 ──
    lines.append("━━ *포트폴리오 구성* ━━")
    for key, det in port_result["details"].items():
        pct = det["value"] / port_result["total"] * 100 if port_result["total"] > 0 else 0
        lines.append(f"{det['name']}: {format_억(det['value'])} ({pct:.0f}%)")
    lines.append("")

    if not port_eval["fired_now"] and not any(e.get("fired_now") for e in stock_evals.values()):
        if semi_ratio <= SEMI_TARGET_RATIO:
            lines.append("📌 트리거 전 — *행동 없음*. 숫자만 확인.")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 텔레그램 전송
# ──────────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────────

def main():
    state = load_state()

    # 시즌 리셋 체크
    if state.get("season_start") != SEASON_START:
        state = {
            "season_start": SEASON_START,
            "portfolio": {"peak": 0.0, **empty_trigger_state()},
            "stocks": {code: {"peak": 0.0, **empty_trigger_state()} for code in SEMI_CODES},
        }

    # 가격 수집
    kr_prices = {}
    for code in KR_POSITIONS:
        try:
            kr_prices[code] = fetch_kr_price(code)
            print(f"[OK] {KR_POSITIONS[code]['name']}: {kr_prices[code]:,.0f}원")
        except Exception as e:
            print(f"[오류] {code} 가격 수집 실패: {e}")

    us_prices = {}
    usdkrw = 1380.0  # 기본값 (환율 수집 실패 시)
    try:
        usdkrw = fetch_usdkrw()
        print(f"[OK] USD/KRW: {usdkrw:,.1f}")
    except Exception as e:
        print(f"[경고] 환율 수집 실패, 기본값 {usdkrw} 사용: {e}")

    for ticker in US_POSITIONS:
        try:
            us_prices[ticker] = fetch_us_price(ticker)
            val_krw = us_prices[ticker] * usdkrw
            print(f"[OK] {US_POSITIONS[ticker]['name']}: ${us_prices[ticker]:,.2f} (≈{val_krw:,.0f}원)")
        except Exception as e:
            print(f"[오류] {ticker} 가격 수집 실패: {e}")

    if not kr_prices:
        print("[오류] 한국 주식 가격을 하나도 못 가져옴 — 전송 생략")
        return

    # 포트폴리오 계산
    port_result = calc_portfolio(kr_prices, us_prices, usdkrw)
    print(f"\n[포트폴리오] 총 {format_억(port_result['total'])}, 반도체 비중 {port_result['semi_ratio']*100:.1f}%")

    # 1단계: 포트폴리오 트리거 평가
    port_state = state.setdefault("portfolio", {"peak": 0.0, **empty_trigger_state()})
    # fired/streak 키 보정 (이전 state와 호환)
    for key in ["streak", "fired"]:
        if key not in port_state:
            port_state[key] = {t["label"]: (0 if key == "streak" else False) for t in TRIGGERS}
    port_eval = evaluate_triggers(port_result["total"], port_state)

    # 2단계: 종목별 트리거 평가
    stock_evals = {}
    for code in SEMI_CODES:
        if code not in kr_prices:
            continue
        st = state["stocks"].setdefault(code, {"peak": 0.0, **empty_trigger_state()})
        for key in ["streak", "fired"]:
            if key not in st:
                st[key] = {t["label"]: (0 if key == "streak" else False) for t in TRIGGERS}
        stock_evals[code] = evaluate_triggers(kr_prices[code], st)

    # 메시지 생성 & 전송
    msg = build_message(port_result, port_eval, stock_evals, port_result["semi_ratio"])
    send_telegram(msg)
    save_state(state)


if __name__ == "__main__":
    main()

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
