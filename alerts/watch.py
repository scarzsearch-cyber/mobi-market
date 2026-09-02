#!/usr/bin/env python3
"""탭을 닫아도 도는 감시기 — GitHub Actions 에서 주기적으로 실행된다.

index.html 은 브라우저 탭이 열려 있어야만 감시가 돌아간다(가이드 6번). 이 스크립트는
그 한계를 없애기 위한 서버측 경로다: 저장소에 커밋된 settings.json 을 읽어 모비 API 를
찔러보고, 조건이 맞으면 카카오톡 "나에게 보내기"로 알린다.

★ 보안 규약 (공개 저장소 = Actions 로그도 공개다)
  - 시크릿에서 파생된 어떤 값도 출력하지 않는다. GitHub 의 자동 마스킹은 "글자 그대로
    같을 때"만 걸리므로, 잘리거나 인코딩된 형태는 ***로 안 가려진다.
  - 그래서 이 스크립트는 응답 본문·헤더·환경변수를 통째로 찍는 코드를 두지 않는다.
    오류를 알릴 때도 상태코드와 우리가 쓴 문구만 남긴다.

★ 브라우저와 이 스크립트의 역할 분담
  - 여기서 하는 것: 천장 기준 신호(천장근접·저가·되사기), 품귀, 저가하락·저가상승(% 변동), 목표가.
    전부 "최신 스냅샷 하나 + 직전 값 + 사람이 입력한 평균가"만으로 판정되는 것들이라
    서버에서 화면과 똑같이 재현된다. 판정 규약은 index.html 과 같다(bandPosPct/rebuyInfo).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = "https://open.mabimobi.life/v1"
KST = timezone(timedelta(hours=9))

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(HERE, "settings.json")
STATE_PATH = os.path.join(HERE, "state.json")

# 서버 한도는 분당 30회. 여유를 두고 요청 사이에 간격을 준다(index.html 의 API_CALL_GAP_MS 와 같은 취지).
REQUEST_GAP_SEC = 2.5
HTTP_TIMEOUT = 20

# 같은 아이템으로 연달아 카톡이 쏟아지지 않게 하는 기본 쿨다운(분).
# settings.json 에 값이 있으면 그쪽이 이긴다.
DEFAULT_ITEM_COOLDOWN_MIN = 30


def log(msg):
    """진행 상황만 남긴다 — 시크릿에서 파생된 값은 절대 여기로 오지 않는다."""
    print(msg, flush=True)


# ---------------------------------------------------------------- HTTP

def _request(url, *, method="GET", headers=None, data=None, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 본문을 로그로 흘리지 않는다 — 상태코드만 올린다.
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = None
        return e.code, body
    except Exception as e:
        return None, {"_transport_error": type(e).__name__}


# ---------------------------------------------------------------- API 키 로테이션

# 키 하나당 분당 30회 한도가 걸린다. 감시 아이템이 늘면 한 주기에 그만큼 요청이 나가고,
# 무엇보다 화면(index.html)이 같은 키를 쓰고 있으면 둘이 한 키의 한도를 나눠 쓰게 되어
# 정작 사람이 보는 화면에서 429 가 뜬다. 그래서 여기도 키를 여러 개 받아 돌려 쓴다.
#
# MOBI_API_KEY 시크릿에 줄바꿈이나 쉼표로 여러 개를 넣으면 자동으로 다 쓴다.
# ★ 화면에서 쓰는 키와는 다른 키를 주는 게 가장 깔끔하다 — 아예 경쟁하지 않는다.
class KeyPool:
    def __init__(self, raw):
        import re
        self.keys = [k.strip() for k in re.split(r"[\s,]+", raw or "") if k.strip()]
        self.idx = 0
        self.dead = set()  # 401 이 난 키 — 이번 실행 동안만 제외한다

    def alive(self):
        return [k for k in self.keys if k not in self.dead]

    def take(self):
        alive = self.alive()
        if not alive:
            return None
        key = alive[self.idx % len(alive)]
        self.idx += 1
        return key


def fetch_prices(pool, name):
    """이름으로 현재 매물 스냅샷을 받는다 (index.html 의 refreshWatchItem 과 같은 엔드포인트).

    429(한도)나 401(무효 키)이 나오면 다음 키로 넘어가 재시도한다. 키를 한 바퀴 다 돌아도
    안 되면 그때 실패로 보고한다 — 키가 5개인데 한 개가 막혔다고 조회를 포기하면 안 된다.
    """
    url = f"{BASE_URL}/market/prices?search={urllib.parse.quote(name)}&limit=20"
    attempts = max(1, len(pool.alive()))
    last_status = None
    for _ in range(attempts):
        key = pool.take()
        if key is None:
            break
        status, body = _request(url, headers={"Accept": "application/json",
                                              "Authorization": "Bearer " + key})
        last_status = status
        if status == 401:
            pool.dead.add(key)  # 무효 키는 이번 실행 내내 건너뛴다
            continue
        if status == 429:
            time.sleep(1)       # 다음 키로 넘어가되 잠깐 숨을 돌린다
            continue
        if status != 200 or not isinstance(body, dict):
            return status, None
        return status, body.get("data") or []
    return last_status, None


def fetch_history(pool, kind_id, days=7):
    """시세이력(OHLC + 수량 OHLC). 평균가 추정용. 키 로테이션은 fetch_prices 와 같다."""
    url = f"{BASE_URL}/market/prices/history?kind_id={kind_id}&days={days}"
    attempts = max(1, len(pool.alive()))
    for _ in range(attempts):
        key = pool.take()
        if key is None:
            break
        status, body = _request(url, headers={"Accept": "application/json",
                                              "Authorization": "Bearer " + key})
        if status == 401:
            pool.dead.add(key)
            continue
        if status == 429:
            time.sleep(1)
            continue
        if status != 200 or not isinstance(body, dict):
            return None
        return body.get("points") or []
    return None


# ---------------------------------------------------------------- 평균가 추정 (index.html effectiveAvg 와 같은 규약)

# 게임 평균은 API 로 정확히 재현되지 않는다(체결가 vs 최저 매물, 아이템별 최대 15% 차이).
# 세 겹: ① 72h 종가를 팔린 수량으로 가중 평균 ② 사용자가 입력해 둔 보정 계수를 곱한다
# ③ 최근 6h max(high)/1.5 는 평균의 확실한 하한이라 그 밑이면 끌어올린다.
# ★ 이 세 상수와 우선순위는 index.html 과 반드시 같아야 한다 — 화면과 카톡이 다른 천장을
#   말하면 안 된다. 바꿀 때는 양쪽을 같이 바꾸고 test_rules [13] 을 갱신한다.
EST_WINDOW_H = 72
EST_FLOOR_WINDOW_H = 6
EST_MIN_POINTS = 6
CEILING_MULT = 1.5
AVG_STALE_SEC = 12 * 3600
RATIO_STALE_SEC = 7 * 24 * 3600


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def estimate_avg(points, now_ts):
    """(추정 평균, 관측 하한, 표본 수). 부족하면 (None, None, n)."""
    t0 = now_ts - EST_WINDOW_H * 3600
    tf = now_ts - EST_FLOOR_WINDOW_H * 3600
    win = [p for p in (points or []) if p.get("close") is not None and p.get("time") and _ts(p["time"]) >= t0]
    if len(win) < EST_MIN_POINTS:
        return None, None, len(win)
    wsum = psum = 0.0
    for p in win:
        sold = max(0, (p.get("count_open") or 0) - (p.get("count_close") or 0))
        wsum += sold
        psum += p["close"] * sold
    est = psum / wsum if wsum > 0 else sum(p["close"] for p in win) / len(win)
    recent = [p["high"] for p in win if p.get("high") is not None and _ts(p["time"]) >= tf]
    floor_avg = (max(recent) / CEILING_MULT) if recent else None
    return est, floor_avg, len(win)


def effective_avg(w, est, floor_avg, now_ts):
    """우선순위: 12h 안 입력값 > 추정×계수 > 미보정 추정 > 옛 입력. 하한은 항상 적용.
    반환 (avg, source, recalib). avg 가 None 이면 천장 신호를 낼 수 없다."""
    typed = w.get("avgPrice")
    typed_at = (w.get("avgPriceAt") or 0) / 1000.0  # ms → s
    ratio = w.get("avgRatio")
    ratio_at = (w.get("avgRatioAt") or 0) / 1000.0
    avg = None; source = None; recalib = False
    if typed and typed > 0 and typed_at and (now_ts - typed_at) <= AVG_STALE_SEC:
        avg, source = typed, "입력"
    elif est and ratio and ratio > 0:
        avg, source = est * ratio, "보정"
        if not ratio_at or (now_ts - ratio_at) > RATIO_STALE_SEC:
            recalib = True
    elif est:
        avg, source, recalib = est, "미보정", True
    elif typed and typed > 0:
        avg, source, recalib = typed, "옛입력", True
    if avg is not None and floor_avg and floor_avg > avg * 1.001:
        avg, source, recalib = floor_avg, source + "·하한", True
    return avg, source, recalib


# ---------------------------------------------------------------- 카카오

def kakao_access_token(rest_key, refresh_token):
    """refresh_token 으로 access_token 을 받아온다.

    반환: (access_token, new_refresh_token_or_None, error_or_None)
    카카오는 refresh_token 의 남은 유효기간이 1개월 미만일 때만 새 refresh_token 을 같이 준다.
    그 경우 저장소 시크릿을 사람이 갈아줘야 하므로(액션이 시크릿을 못 쓴다) 신호만 올린다.
    """
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": rest_key,
        "refresh_token": refresh_token,
    }).encode("utf-8")
    status, body = _request(
        "https://kauth.kakao.com/oauth/token",
        method="POST", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        # body 를 찍지 않는다 — 오류 응답에 토큰 조각이 섞여 올 수 있다.
        return None, None, f"카카오 토큰 갱신 실패 (HTTP {status})"
    return body["access_token"], body.get("refresh_token"), None


def kakao_send(access_token, text, link_url):
    template = {
        "object_type": "text",
        "text": text[:900],  # 카카오 텍스트 템플릿 상한(1000자)보다 여유 있게 자른다
        "link": {"web_url": link_url, "mobile_web_url": link_url},
        "button_title": "차트 보기",
    }
    data = urllib.parse.urlencode({"template_object": json.dumps(template, ensure_ascii=False)}).encode("utf-8")
    status, _ = _request(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        method="POST", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                 "Authorization": "Bearer " + access_token},
    )
    return status == 200, status


# ---------------------------------------------------------------- GitHub 이슈 알림 (카톡 없이)

# 카카오는 개발자 앱을 만들고 로그인까지 해야 쓸 수 있고, refresh 토큰이 두 달마다
# 만료돼서 사람이 다시 갈아줘야 한다. 그것 때문에 알림을 아예 못 받는 상태로 두느니,
# 자격증명이 하나도 더 필요 없는 통로를 기본으로 둔다 — 저장소 이슈에 댓글을 달면
# GitHub 이 메일을 보내주고, 그 메일이 폰으로 온다. GITHUB_TOKEN 은 워크플로가 이미
# 갖고 있으므로 사람이 등록할 시크릿이 늘지 않는다.
#
# 이슈를 매번 새로 만들지 않고 하나를 재사용한다 — 알림마다 이슈가 쌓이면 목록이 못 쓰게 된다.
ISSUE_TITLE = "🔔 가격 알림"
ISSUE_MARKER = "<!-- mobi-market-alert-thread -->"


def _gh(url, token, method="GET", payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    return _request(url, method=method, data=data, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "mobi-market-watch",
    })


def find_or_create_issue(token, repo):
    """알림을 모아둘 이슈 하나를 찾거나 만든다. 실패하면 None."""
    status, body = _gh(f"https://api.github.com/repos/{repo}/issues?state=open&per_page=100", token)
    if status == 200 and isinstance(body, list):
        for it in body:
            if ISSUE_MARKER in (it.get("body") or ""):
                return it.get("number")
    status, body = _gh(f"https://api.github.com/repos/{repo}/issues", token, "POST", {
        "title": ISSUE_TITLE,
        "body": ISSUE_MARKER + "\n\n가격 알림이 이 이슈에 댓글로 쌓입니다. "
                "댓글이 달리면 GitHub 이 메일로 알려줍니다.\n"
                "이 이슈를 닫으면 새 이슈가 만들어집니다 — 지우지 말고 그냥 두세요.",
    })
    if status in (200, 201) and isinstance(body, dict):
        return body.get("number")
    return None


def github_issue_notify(token, repo, owner, number, text, link_url):
    # 소유자를 멘션해야 알림 메일이 확실히 간다 (저장소 구독 설정과 무관하게).
    comment = f"@{owner}\n\n{text}\n\n[차트 보기]({link_url})"
    status, _ = _gh(f"https://api.github.com/repos/{repo}/issues/{number}/comments",
                    token, "POST", {"body": comment})
    return status in (200, 201), status


# ---------------------------------------------------------------- 설정/상태

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log(f"⚠ {os.path.basename(path)} 를 읽지 못했습니다: {type(e).__name__}")
        return default


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def in_quiet_hours(quiet, now_kst):
    """index.html 의 isQuietHours() 와 같은 규약 — 자정을 넘는 구간도 처리한다."""
    if not quiet or not quiet.get("enabled"):
        return False
    try:
        sh, sm = (int(x) for x in str(quiet.get("start", "23:00")).split(":"))
        eh, em = (int(x) for x in str(quiet.get("end", "08:00")).split(":"))
    except Exception:
        return False
    cur = now_kst.hour * 60 + now_kst.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end  # 자정을 넘는 구간


# ---------------------------------------------------------------- 판정

def evaluate(item, prev, w, alerts):
    """이 아이템에서 이번에 울려야 할 알림 문구들을 만든다.

    item : 이번 스냅샷 (API 응답 한 건)
    prev : 지난 실행 때의 값 {price, count, streak_low, streak_high, peak, fired}
    w    : settings.json 의 감시 항목 (index.html slimWatchItem 형식 그대로)
    alerts: 이 아이템에 걸린 목표가/트레일링 알림들 (alertsByKind[kind_id])

    반환: (메시지 리스트, 다음 실행에 넘길 상태)
    """
    msgs = []
    name = w.get("name") or item.get("name") or "?"
    sold_out = bool(item.get("is_sold_out"))
    price = None if sold_out else item.get("min_price")
    count = item.get("total_count")

    prev_price = prev.get("price")
    prev_count = prev.get("count")
    prev_sold_out = prev.get("soldOut", False)

    state = {"price": price, "count": count, "soldOut": sold_out,
             "streak_low": 0, "streak_high": 0,
             "fired": dict(prev.get("fired") or {})}

    def fmt(n):
        return f"{n:,}" if isinstance(n, (int, float)) else "—"

    # ── 품귀 (직전 대비 수량 감소) ──
    # 첫 실행이면 비교 기준이 없으므로 건너뛴다 (index.html 과 같은 규약).
    if prev_count is not None and count is not None:
        if w.get("listingDropAlert") and (prev_count - count) >= (w.get("listingDropThreshold") or 1):
            msgs.append(f"⚠ {name} 매물 {fmt(prev_count)} → {fmt(count)}개 — 품귀 진행 중, 팔기 좋은 타이밍일 수 있어요")

    # ── 최저가 하락/상승 ──
    # 품절↔매물있음 은 "가격 변동"이 아니라 "상태 변화"라 비교 대상이 아니다.
    if (not prev_sold_out and not sold_out
            and prev_price not in (None, 0) and price is not None):
        delta_pct = (price - prev_price) / prev_price * 100
        is_drop = delta_pct < 0 and abs(delta_pct) >= (w.get("lowestPriceThresholdPct") or 0)
        is_rise = delta_pct > 0 and delta_pct >= (w.get("highestPriceThresholdPct") or 0)

        if w.get("lowestPriceAlert"):
            if is_drop:
                streak = (prev.get("streak_low") or 0) + 1
                if streak >= (w.get("lowestPriceConfirm") or 1):
                    msgs.append(f"⬇ {name} 최저가 {fmt(prev_price)} → {fmt(price)} ({delta_pct:+.1f}%) — 지금이 살 타이밍일 수 있어요")
                    streak = 0
                state["streak_low"] = streak
        if w.get("highestPriceAlert"):
            if is_rise:
                streak = (prev.get("streak_high") or 0) + 1
                if streak >= (w.get("highestPriceConfirm") or 1):
                    msgs.append(f"⬆ {name} 최저가 {fmt(prev_price)} → {fmt(price)} ({delta_pct:+.1f}%) — 지금이 팔 타이밍일 수 있어요")
                    streak = 0
                state["streak_high"] = streak

    # ── 목표가(threshold) / 트레일링 ──
    # index.html 의 checkPriceAlerts 와 같은 규약: 한 번 울리면 조건을 벗어나야 재무장한다.
    if price is not None:
        for a in alerts:
            aid = str(a.get("id"))
            atype = a.get("type") or "threshold"

            if atype != "threshold":
                continue  # 목표가만 남았다 — 예전 형식(trailing/trend_reversal)이 있어도 건너뛴다

            target = a.get("price")
            if target is None:
                continue
            hit = price >= target if a.get("dir") == "above" else price <= target
            margin = (a.get("marginPct") or 0) / 100
            if hit:
                if not state["fired"].get(aid):
                    state["fired"][aid] = True
                    word = "이상" if a.get("dir") == "above" else "이하"
                    msgs.append(f"💰 {name} {fmt(price)} — 목표가 {fmt(target)} {word} 도달")
            else:
                released = (price <= target * (1 - margin)) if a.get("dir") == "above" \
                    else (price >= target * (1 + margin))
                if state["fired"].get(aid) and released:
                    state["fired"].pop(aid, None)

    # ── 천장 기준 신호 (v2) — index.html 의 bandPosPct / rebuyInfo 와 같은 규약 ──
    # 판매가 상한 = 3일 평균 x 1.5 (소유자 실측). 평균가는 사람이 앱에 입력해 settings.json 으로
    # 넘어온다. 한 번 울리면 기준에서 10%p 벗어나야 재무장한다 (경계선 진동 방지).
    # main() 이 추정·보정·하한을 거친 유효 평균을 avgPrice_effective 로 넣어 준다.
    # (직접 호출하는 테스트는 avgPrice 만 줘도 된다.)
    avg = w.get("avgPrice_effective") if w.get("avgPrice_effective") else w.get("avgPrice")
    if avg and avg > 0 and price is not None:
        ceiling = round(avg * CEILING_MULT)
        pos = price / ceiling * 100
        if w.get("sellHighAlert"):
            th = w.get("sellHighPct") or 90
            if pos >= th and not state["fired"].get("sellHigh"):
                state["fired"]["sellHigh"] = True
                msgs.append(f"🔴 {name} 최저가 {fmt(price)} = 천장({fmt(ceiling)})의 {pos:.0f}% — 물량이 말랐어요. 천장 근처에 걸어두면 팔릴 때예요")
            elif state["fired"].get("sellHigh") and pos < th - 10:
                state["fired"].pop("sellHigh", None)
        if w.get("buyLowAlert"):
            th = w.get("buyLowPct") or 60
            if pos <= th and not state["fired"].get("buyLow"):
                state["fired"]["buyLow"] = True
                msgs.append(f"🟢 {name} 최저가 {fmt(price)} = 천장({fmt(ceiling)})의 {pos:.0f}% — 천장 대비 싸요. 살 때예요")
            elif state["fired"].get("buyLow") and pos > th + 10:
                state["fired"].pop("buyLow", None)
    sell = w.get("mySellPrice")
    if w.get("rebuyAlert") and sell and sell > 0 and price is not None:
        net = sell * 0.85
        profit = net - price
        if profit > 0 and not state["fired"].get("rebuy"):
            state["fired"]["rebuy"] = True
            msgs.append(f"🔁 {name} 최저가 {fmt(price)} < 내 판매가 {fmt(sell)}의 85%({fmt(round(net))}) — 지금 되사면 개당 +{fmt(round(profit))}")
        elif state["fired"].get("rebuy") and profit <= 0:
            state["fired"].pop("rebuy", None)

    return msgs, state


# ---------------------------------------------------------------- main

def main():
    api_key = os.environ.get("MOBI_API_KEY", "").strip()
    rest_key = os.environ.get("KAKAO_REST_KEY", "").strip()
    refresh_token = os.environ.get("KAKAO_REFRESH_TOKEN", "").strip()
    page_url = os.environ.get("PAGE_URL", "https://scarzsearch-cyber.github.io/mobi-market/").strip()

    # 설정을 먼저 읽는다 — 시크릿 등록 전이라도 "설정 파일이 제대로 읽히는지"를 워크플로
    # 로그에서 확인할 수 있어야 한다. 순서가 반대면 시크릿을 넣기 전까지 설정이 맞는지
    # 알 방법이 없고, 둘 다 처음 하는 사람은 뭐가 문제인지 못 가린다.
    settings = load_json(SETTINGS_PATH, None)
    if not settings or not isinstance(settings.get("watchList"), list):
        log("⏭ alerts/settings.json 이 없거나 형식이 아닙니다 — 앱의 [GitHub에 올리기] 버튼으로 만들 수 있어요.")
        return 0
    # kind_id 는 없어도 된다 — 이름만 있으면 조회 결과에서 정확히 일치하는 것을 찾아 쓴다.
    # 손으로 설정을 적을 때 내부 id 까지 알아야 하는 건 무리다.
    watch_list = [w for w in settings["watchList"] if w.get("name")]
    if not watch_list:
        log("⏭ 감시 목록이 비어 있습니다.")
        return 0
    log(f"설정을 읽었습니다 — 감시 대상 {len(watch_list)}개: " + ", ".join(w["name"] for w in watch_list[:10]))

    # 사람이 반드시 넣어야 하는 건 API 키 하나뿐이다 — 그게 없으면 시세를 못 읽으니 대안이 없다.
    pool = KeyPool(api_key)
    if not pool.keys:
        log("⏭ MOBI_API_KEY 가 없어서 여기서 멈춥니다.")
        log("   저장소 Settings → Secrets and variables → Actions 에서 등록하면 바로 돌기 시작해요.")
        return 0  # 실패가 아니라 "아직 설정 전" — 빨간 X 를 상시로 만들지 않는다
    log(f"API 키 {len(pool.keys)}개를 돌려 씁니다." if len(pool.keys) > 1 else "API 키 1개를 씁니다.")

    # 알림 통로 결정: 카카오가 갖춰져 있으면 그쪽, 아니면 GitHub 이슈 댓글(=메일).
    # 카카오는 앱 등록·로그인이 필요하고 토큰이 두 달마다 만료되므로 기본으로 요구하지 않는다.
    use_kakao = bool(rest_key and refresh_token)
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    gh_owner = gh_repo.split("/")[0] if "/" in gh_repo else ""
    if use_kakao:
        log("알림 통로: 카카오톡")
    elif gh_token and gh_repo:
        log("알림 통로: GitHub 이슈 댓글 → 메일 (카카오는 미설정)")
    else:
        log("⚠ 알림 통로가 없습니다 — 카카오 시크릿도, GITHUB_TOKEN 도 없습니다.")
        return 1

    alerts_by_kind = settings.get("alertsByKind") or {}
    quiet = settings.get("quiet")
    cooldown_min = settings.get("kakaoItemCooldownMin", DEFAULT_ITEM_COOLDOWN_MIN)
    try:
        cooldown_min = max(0, int(cooldown_min))
    except Exception:
        cooldown_min = DEFAULT_ITEM_COOLDOWN_MIN

    state = load_json(STATE_PATH, {})
    items_state = state.get("items") or {}
    now = time.time()
    now_kst = datetime.now(KST)
    quiet_now = in_quiet_hours(quiet, now_kst)

    log(f"감시 {len(watch_list)}개 · {now_kst:%Y-%m-%d %H:%M} KST" + (" · 조용한 시간대" if quiet_now else ""))

    pending = []   # [(kind_id, name, [메시지])]
    api_failures = 0

    for i, w in enumerate(watch_list):
        if i:
            time.sleep(REQUEST_GAP_SEC)
        status, data = fetch_prices(pool, w["name"])
        if data is None:
            api_failures += 1
            log(f"  · {w['name']}: 조회 실패 (HTTP {status})")
            continue

        kind_id = w.get("kind_id")
        if kind_id:
            found = next((d for d in data if d.get("kind_id") == kind_id), None)
        else:
            # id 를 모르면 이름이 정확히 일치하는 것만 받아들인다. 여러 개가 걸리면
            # 임의로 하나를 고르지 않고 건너뛴다 — 엉뚱한 아이템을 감시하느니 안 하는 게 낫다.
            exact = [d for d in data if (d.get("name") or "") == w["name"]]
            if len(exact) > 1:
                log(f"  · {w['name']}: 같은 이름이 {len(exact)}개라 특정할 수 없어요 (kind_id 를 적어주세요)")
                continue
            found = exact[0] if exact else None
            if found:
                kind_id = found.get("kind_id")
        if not found:
            log(f"  · {w['name']}: 스냅샷에 없음")
            continue
        if not w.get("kind_id"):
            log(f"  · {w['name']}: 이름으로 찾음 (kind_id={kind_id})")

        key = str(kind_id)
        prev = items_state.get(key) or {}
        w_eff = dict(w)
        if w.get("sellHighAlert") or w.get("buyLowAlert"):
            # 천장 신호가 켜진 아이템만 이력을 한 번 더 받는다 (아이템당 요청 +1).
            time.sleep(REQUEST_GAP_SEC)
            pts = fetch_history(pool, kind_id, 7)
            est, floor_avg, n = estimate_avg(pts, now)
            avg, source, recalib = effective_avg(w, est, floor_avg, now)
            if avg:
                w_eff["avgPrice_effective"] = avg
                log(f"  · {w['name']}: 평균 {avg:,.1f} ({source}{' ⏰' if recalib else ''}) 천장 {round(avg * CEILING_MULT):,} · 표본 {n}")
            else:
                log(f"  · {w['name']}: 평균가 없음(이력 {n}개) — 천장 신호 건너뜀")
        msgs, new_state = evaluate(found, prev, w_eff, alerts_by_kind.get(key) or [])
        items_state[key] = new_state
        if msgs:
            pending.append((key, w["name"], msgs))

    # ── 알림 전송 (조용한 시간대엔 억제 — index.html 과 같은 규약) ──
    sent_state = state.get("lastSentAt") or {}
    sent_count = 0
    token_note = None

    if pending and not quiet_now:
        due = []
        for key, name, msgs in pending:
            last = sent_state.get(key) or 0
            if cooldown_min and (now - last) < cooldown_min * 60:
                log(f"  · {name}: 쿨다운({cooldown_min}분) 중이라 생략")
                continue
            due.append((key, name, msgs))

        if due:
            # 통로별로 한 번만 준비한다 (토큰 교환·이슈 조회를 아이템마다 반복하지 않게).
            access = issue_no = None
            if use_kakao:
                access, new_refresh, err = kakao_access_token(rest_key, refresh_token)
                if err:
                    log("⚠ " + err)
                    return 1  # 알림 채널이 죽은 건 진짜 실패 — 메일이 오게 둔다
                if new_refresh and new_refresh != refresh_token:
                    token_note = ("♻ 카카오가 새 refresh 토큰을 발급했습니다 — 시크릿 "
                                  "KAKAO_REFRESH_TOKEN 을 갱신하지 않으면 두 달 안에 알림이 멈춥니다.")
            else:
                issue_no = find_or_create_issue(gh_token, gh_repo)
                if not issue_no:
                    log("⚠ 알림용 이슈를 만들지 못했습니다 — 저장소 Issues 기능이 꺼져 있는지 확인해주세요.")
                    return 1

            for key, name, msgs in due:
                text, link = "\n".join(msgs), page_url + "?open=" + key
                if use_kakao:
                    ok, st = kakao_send(access, text, link)
                    where = "카톡"
                else:
                    ok, st = github_issue_notify(gh_token, gh_repo, gh_owner, issue_no, text, link)
                    where = "이슈 댓글"
                if ok:
                    sent_state[key] = now
                    sent_count += 1
                    log(f"  ✓ {name}: {where} {len(msgs)}건 전송")
                else:
                    log(f"  ⚠ {name}: {where} 전송 실패 (HTTP {st})")
    elif pending:
        log(f"  · 조용한 시간대라 {len(pending)}개 아이템의 알림을 억제했습니다(상태는 갱신).")

    state["items"] = items_state
    state["lastSentAt"] = sent_state
    state["lastRunAt"] = now_kst.isoformat(timespec="seconds")
    save_state(state)

    log(f"완료 — 조건 충족 {len(pending)}개 · 전송 {sent_count}건 · 조회 실패 {api_failures}건")
    if token_note:
        log(token_note)
    # 전부 실패했으면 진짜 문제(키 만료 등) — 실패로 올려서 메일이 오게 한다
    if pool.dead:
        log(f"⚠ 무효(401)로 확인된 키 {len(pool.dead)}개를 이번 실행에서 제외했습니다 — 시크릿을 확인해주세요.")
    if api_failures and api_failures == len(watch_list):
        log("⚠ 모든 조회가 실패했습니다 — API 키가 만료됐거나 한도에 걸렸을 수 있습니다.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
