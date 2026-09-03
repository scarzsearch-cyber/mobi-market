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
# state.json 과 달리 이 파일만 main 에 올라간다 — 브라우저가 읽어야 하기 때문. save_learned() 참고.
LEARNED_PATH = os.path.join(HERE, "learned.json")

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
# 네 겹: ① 직전 갱신 시각까지의 72h 종가를 팔린 수량으로 가중 평균 ② plateau 자동보정 — 최근 12h max(high) 에
# 3캔들 이상 머물렀고 그 시간대 수량이 창 중앙값의 절반 이하(=정말 말랐다)이며 역산 평균이
# 추정의 0.75~1.3 배 안이면 그 값을 평균으로 채택하고 계수를 학습 ③ 저장된 보정 계수를 곱한다
# ④ 최근 6h max(high)/1.5 는 평균의 확실한 하한이라 그 밑이면 끌어올린다.
# ★ 상수·우선순위·판정식은 index.html 과 반드시 같아야 한다 — 화면과 카톡이 다른 천장을
#   말하면 안 된다. 바꿀 때는 양쪽을 같이 바꾸고 test_rules [13] 을 갱신한다.
EST_WINDOW_H = 72
EST_FLOOR_WINDOW_H = 6
EST_PLATEAU_WINDOW_H = 12
EST_MIN_POINTS = 6
PLATEAU_MIN_CANDLES = 3
PLATEAU_COUNT_RATIO_MAX = 0.5
PLATEAU_BAND = (0.75, 1.3)
CEILING_MULT = 1.5
# 재매입은 수수료(15%)만 겨우 넘기는 수준(이득 0에 가까움)은 표기하지 않는다 —
# 순수령(net) 대비 최소 이만큼은 남아야 "재매입할 가치"로 본다 (소유자 지정).
REBUY_MARGIN_PCT = 10

# 품귀 판정 — index.html 의 SCARCE_*/COUNT_MIN_SAMPLES 와 반드시 같아야 한다.
# "직전 실행보다 N개 줄었다"가 아니라 "이 아이템의 최근 7일 수량 분포에서 바닥권인가"로 본다.
PCTL_WINDOW_DAYS = 7
COUNT_MIN_SAMPLES = 12
SCARCE_ENTER_PCTL = 20
SCARCE_EXIT_PCTL = 40

# ---- 평균가가 다시 매겨지는 시각: 매일 06:00 · 18:00 KST (소유자 실측 2026-09-04) ----
# ★ index.html 의 AVG_RESET_HOURS_KST / lastAvgResetMs 와 반드시 같아야 한다.
# 종전에는 "입력한 지 12시간"이라는 슬라이딩 창이었다. 실제 갱신 시각을 알고 나면 그건 틀린다 —
# 17:50 에 넣은 값은 10분 뒤 갱신되며 낡는데 슬라이딩 창은 11시간 50분을 더 싱싱하다고 본다.
AVG_RESET_HOURS_KST = (6, 18)
KST = timezone(timedelta(hours=9))


def last_avg_reset(now_ts):
    """지금 걸려 있는 평균가가 매겨진 시각(직전 06:00 또는 18:00 KST)의 epoch 초."""
    k = datetime.fromtimestamp(now_ts, KST)
    midnight = k.replace(hour=0, minute=0, second=0, microsecond=0)
    passed = [h for h in AVG_RESET_HOURS_KST if h <= k.hour]
    if not passed:
        return (midnight - timedelta(days=1)).timestamp() + AVG_RESET_HOURS_KST[-1] * 3600
    return midnight.timestamp() + max(passed) * 3600


def next_avg_reset(now_ts):
    """다음 갱신 시각의 epoch 초."""
    k = datetime.fromtimestamp(now_ts, KST)
    midnight = k.replace(hour=0, minute=0, second=0, microsecond=0)
    upcoming = [h for h in AVG_RESET_HOURS_KST if h > k.hour]
    if not upcoming:
        return (midnight + timedelta(days=1)).timestamp() + AVG_RESET_HOURS_KST[0] * 3600
    return midnight.timestamp() + min(upcoming) * 3600


# 계수는 마지막 값으로 덮어쓰지 않고 절반씩 당긴다 — index.html 의 RATIO_LEARN_ALPHA 와 같아야 한다.
RATIO_LEARN_ALPHA = 0.5


def blend_ratio(old_ratio, sample):
    if not sample or sample <= 0:
        return None
    if not old_ratio or old_ratio <= 0:
        return sample
    return old_ratio * (1 - RATIO_LEARN_ALPHA) + sample * RATIO_LEARN_ALPHA


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _median(xs):
    import statistics
    return statistics.median(xs) if xs else None


def estimate_avg(points, now_ts):
    """(추정 평균, 관측 하한, plateau 평균, 표본 수). 부족하면 (None, None, None, n)."""
    anchor = last_avg_reset(now_ts)          # 지금 걸려 있는 평균이 매겨진 시각
    t0 = anchor - EST_WINDOW_H * 3600        # 그 평균이 실제로 본 3일 창의 시작
    tf = now_ts - EST_FLOOR_WINDOW_H * 3600
    tp = now_ts - EST_PLATEAU_WINDOW_H * 3600
    win = [p for p in (points or []) if p.get("close") is not None and p.get("time") and _ts(p["time"]) >= t0]
    if len(win) < EST_MIN_POINTS:
        return None, None, None, len(win)
    # ★ 추정 창의 끝은 "지금"이 아니라 "직전 갱신 시각"이다 — 갱신 뒤 거래는 지금 걸려 있는
    #   평균에 아직 반영되지 않았다. 창을 맞추면 추정이 12시간 동안 고정돼 실제 평균과 같은
    #   계단이 되고, 계수(입력÷추정)가 매번 같은 것을 재는 값이 된다. 표본이 모자라면 물러선다.
    aligned = [p for p in win if _ts(p["time"]) <= anchor]
    est_win = aligned if len(aligned) >= EST_MIN_POINTS else win
    wsum = psum = 0.0
    for p in est_win:
        sold = max(0, (p.get("count_open") or 0) - (p.get("count_close") or 0))
        wsum += sold
        psum += p["close"] * sold
    est = psum / wsum if wsum > 0 else sum(p["close"] for p in est_win) / len(est_win)
    # 하한·plateau 는 "지금 천장이 어디냐"를 관측하는 값이라 기준이 현재다 — 창을 옮기지 않는다.
    recent = [p["high"] for p in win if p.get("high") is not None and _ts(p["time"]) >= tf]
    floor_avg = (max(recent) / CEILING_MULT) if recent else None
    plateau_avg = None
    pw = [p for p in win if p.get("high") is not None and _ts(p["time"]) >= tp]
    if len(pw) >= EST_MIN_POINTS:
        mx = max(p["high"] for p in pw)
        plat = [p for p in pw if p["high"] == mx]
        med_all = _median([p["count_close"] for p in pw if p.get("count_close") is not None])
        med_plat = _median([p["count_close"] for p in plat if p.get("count_close") is not None])
        implied = mx / CEILING_MULT
        if (len(plat) >= PLATEAU_MIN_CANDLES and med_all and med_plat is not None
                and med_plat / med_all <= PLATEAU_COUNT_RATIO_MAX
                and est > 0 and PLATEAU_BAND[0] <= implied / est <= PLATEAU_BAND[1]):
            plateau_avg = implied
    return est, floor_avg, plateau_avg, len(est_win)


def count_percentile(points, count, now_ts):
    """최근 7일 count_close 분포에서 지금 수량의 백분위. 표본이 모자라면 None.

    ★ index.html 의 countPercentile 과 같은 규약이어야 한다 — 화면의 수량 색과 카톡의
      품귀 알림이 서로 다른 말을 하면 안 된다.
      수량은 정수라 같은 값이 자주 겹치므로 동점을 절반으로 세는 표준 백분위 순위를 쓴다.
      (그냥 "미만 개수"로 세면 평소값과 같을 때 하위 0%로 튀어 헛알림이 나간다.)
    """
    if count is None:
        return None
    cutoff = now_ts - PCTL_WINDOW_DAYS * 86400
    vals = []
    for p in points or []:
        c = p.get("count_close")
        if c is None or not p.get("time"):
            continue
        if _ts(p["time"]) < cutoff:
            continue
        vals.append(c)
    if len(vals) < COUNT_MIN_SAMPLES:
        return None
    below = sum(1 for v in vals if v < count)
    equal = sum(1 for v in vals if v == count)
    return (below + equal / 2) / len(vals) * 100


def pick_ratio(w, learned):
    """settings.json 의 계수(사람 입력 또는 앱의 자동 학습)와 state.json 의 서버 학습 계수 중 더 최근 것."""
    cands = []
    if w.get("avgRatio") and w["avgRatio"] > 0:
        cands.append(((w.get("avgRatioAt") or 0) / 1000.0, w["avgRatio"]))
    if learned and learned.get("ratio") and learned["ratio"] > 0:
        cands.append((learned.get("ratioAt") or 0, learned["ratio"]))
    if not cands:
        return None
    return max(cands)[1]


def effective_avg(w, est, floor_avg, plateau_avg, now_ts, learned=None):
    """우선순위: 직전 갱신(06/18시 KST) 뒤 입력값 > plateau 자동보정 > 추정×계수 > 미보정 추정 > 옛 입력. 하한은 항상.
    반환 (avg, source, recalib). recalib 는 근거가 약할 때(미보정·하한)만 True."""
    typed = w.get("avgPrice")
    typed_at = (w.get("avgPriceAt") or 0) / 1000.0
    ratio = pick_ratio(w, learned)
    avg = None; source = None; recalib = False
    if typed and typed > 0 and typed_at and typed_at >= last_avg_reset(now_ts):
        avg, source = typed, "입력"
    elif plateau_avg:
        avg, source = plateau_avg, "자동"
    elif est and ratio:
        avg, source = est * ratio, "보정"
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


def save_learned(items_state):
    """서버가 스스로 배운 보정 계수를, 브라우저가 읽을 수 있는 자리에 따로 남긴다.

    ★ 왜 필요한가: state.json 은 alerts-state 브랜치에만 있어서 Pages 로 서비스되지 않는다.
      그래서 지금까지 서버가 배운 계수는 카톡 알림에만 반영되고 화면은 영영 몰랐다.
      아무도 탭을 안 켜둔 사이에 품귀가 지나가면, 브라우저는 12시간(EST_PLATEAU_WINDOW_H)이
      지난 뒤엔 그 plateau 를 다시 잡을 수 없어서 화면과 카톡이 서로 다른 천장을 말하게 된다.
      이 파일이 그 구멍을 메운다 — main 에 올라가고 index.html 의 pullAvgFromServer 가 읽는다.

    ★ 시각 단위 주의: state.json 의 ratioAt 은 초, index.html 의 avgRatioAt 은 밀리초다.
      브라우저가 자기 값과 그대로 비교(더 최근 것이 이긴다)할 수 있게 밀리초로 바꿔서 내보낸다.
    """
    out = {}
    for key, st in (items_state or {}).items():
        lr = (st or {}).get("learned") or {}
        ratio, at = lr.get("ratio"), lr.get("ratioAt")
        if ratio and ratio > 0 and at:
            out[str(key)] = {"ratio": ratio, "ratioAt": int(at * 1000), "src": lr.get("src") or "auto"}
    with open(LEARNED_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
    return out


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

def evaluate(item, prev, w):
    """이 아이템에서 이번에 울려야 할 알림 문구들을 만든다.

    item : 이번 스냅샷 (API 응답 한 건)
    prev : 지난 실행 때의 값 {price, count, streak_low, streak_high, peak, fired}
    w    : settings.json 의 감시 항목 (index.html slimWatchItem 형식 그대로)

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
             "fired": dict(prev.get("fired") or {})}

    def fmt(n):
        return f"{n:,}" if isinstance(n, (int, float)) else "—"

    # ── 품귀 — 이 아이템 기준으로 매물이 바닥권에 "새로 들어섰을 때" 한 번 ──
    # 옛 규칙("직전 실행보다 N개 줄었다")은 실행 주기에 따라 뜻이 달라지고 아이템마다
    # 임계값을 손으로 잡아야 했다. 지금은 최근 7일 수량 분포의 백분위로 본다 —
    # index.html 의 countPercentile 과 같은 규약이고, 한 번 울리면 회복해야 재무장한다.
    # 백분위는 main() 이 이력에서 계산해 count_pctl 로 넣어준다(없으면 판정하지 않는다).
    pctl = w.get("count_pctl")
    if w.get("listingDropAlert", True) and pctl is not None:
        if pctl >= SCARCE_EXIT_PCTL:
            state["fired"].pop("scarce", None)  # 회복했으니 다시 무장
        elif (pctl <= SCARCE_ENTER_PCTL and not state["fired"].get("scarce")
                and not (w.get("count_change_24h") or 0) > 0):
            # 24h 로 오히려 늘고 있으면 바닥권이어도 품귀가 아니다(기준선이 밀린 경우).
            state["fired"]["scarce"] = True
            avg_eff = w.get("avgPrice_effective") or w.get("avgPrice")
            ceil_txt = f" · 고점 {fmt(round(avg_eff * CEILING_MULT))}" if (avg_eff and avg_eff > 0) else ""
            msgs.append(f"⚠ {name} 매물 {fmt(count)}개 — 최근 7일 중 하위 {pctl:.0f}%로 바닥권"
                        f" (최저가 {fmt(price)}{ceil_txt}) — 마르기 전에 높은 값으로 걸어둘 때")

    # ── 고점 기준 신호 (v2) — index.html 의 bandPosPct / rebuyInfo 와 같은 규약 ──
    # 판매가 상한 = 3일 평균 x 1.5 (소유자 실측). 평균가는 사람이 앱에 입력해 settings.json 으로
    # 넘어온다. 한 번 울리면 기준에서 10%p 벗어나야 재무장한다 (경계선 진동 방지).
    # main() 이 추정·보정·하한을 거친 유효 평균을 avgPrice_effective 로 넣어 준다.
    # (직접 호출하는 테스트는 avgPrice 만 줘도 된다.)
    avg = w.get("avgPrice_effective") if w.get("avgPrice_effective") else w.get("avgPrice")
    if avg and avg > 0 and price is not None:
        ceiling = round(avg * CEILING_MULT)
        pos = price / ceiling * 100
        if w.get("sellHighAlert", True):  # 기본 켜짐(opt-out) — index.html 과 같다
            th = w.get("sellHighPct") or 90
            if pos >= th and not state["fired"].get("sellHigh"):
                state["fired"]["sellHigh"] = True
                msgs.append(f"🔴 {name} 최저가 {fmt(price)} = 고점({fmt(ceiling)})의 {pos:.0f}% — 물량이 말랐어요. 고점 근처에 걸어두면 팔릴 때예요")
            elif state["fired"].get("sellHigh") and pos < th - 10:
                state["fired"].pop("sellHigh", None)
        if w.get("buyLowAlert", True):
            th = w.get("buyLowPct") or 60
            if pos <= th and not state["fired"].get("buyLow"):
                state["fired"]["buyLow"] = True
                msgs.append(f"🟢 {name} 최저가 {fmt(price)} = 고점({fmt(ceiling)})의 {pos:.0f}% — 살 때예요")
            elif state["fired"].get("buyLow") and pos > th + 10:
                state["fired"].pop("buyLow", None)
    sell = w.get("mySellPrice")
    if w.get("rebuyAlert") and sell and sell > 0 and price is not None:
        net = sell * 0.85
        worth_limit = net * (1 - REBUY_MARGIN_PCT / 100)
        profit = net - price
        worth = price <= worth_limit
        if worth and not state["fired"].get("rebuy"):
            state["fired"]["rebuy"] = True
            msgs.append(f"🔁 {name} 최저가 {fmt(price)} ≤ 재매입 기준 {fmt(round(worth_limit))}(내 판매가의 85% 대비 {REBUY_MARGIN_PCT}%↓) — 지금 재매입하면 개당 +{fmt(round(profit))}")
        elif state["fired"].get("rebuy") and not worth:
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
        learned = prev.get("learned") or {}
        # 천장 신호나 품귀가 켜진 아이템만 이력을 한 번 더 받는다 (아이템당 요청 +1).
        # 품귀도 같은 이력(count_close)을 쓰므로 요청은 늘지 않는다.
        if (w.get("sellHighAlert", True) or w.get("buyLowAlert", True)
                or w.get("listingDropAlert", True)):
            time.sleep(REQUEST_GAP_SEC)
            pts = fetch_history(pool, kind_id, 7)
            est, floor_avg, plateau_avg, n = estimate_avg(pts, now)
            if plateau_avg and est:
                # plateau 가 잡히면 계수를 학습해 state 에 남긴다 — 물량이 풀려도 보정이 남는다.
                r = plateau_avg / est
                if not learned.get("ratio") or abs(r / learned["ratio"] - 1) >= 0.05:
                    learned = {"ratio": blend_ratio(learned.get("ratio"), r), "ratioAt": now, "src": "auto"}
            avg, source, recalib = effective_avg(w, est, floor_avg, plateau_avg, now, learned)
            if avg:
                w_eff["avgPrice_effective"] = avg
                log(f"  · {w['name']}: 평균 {avg:,.1f} ({source}{' ⏰' if recalib else ''}) 천장 {round(avg * CEILING_MULT):,} · 표본 {n}")
            else:
                log(f"  · {w['name']}: 평균가 없음(이력 {n}개) — 천장 신호 건너뜀")
            # 품귀 판정에 쓸 수량 백분위 — 화면의 수량 색과 같은 계산이다.
            pctl = count_percentile(pts, found.get("total_count"), now)
            if pctl is not None:
                w_eff["count_pctl"] = pctl
                w_eff["count_change_24h"] = found.get("count_change_24h")
                log(f"    수량 {found.get('total_count')}개 = 하위 {pctl:.0f}%")
        msgs, new_state = evaluate(found, prev, w_eff)
        if learned:
            new_state["learned"] = learned
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
    learned_pub = save_learned(items_state)  # 화면도 같은 천장을 말하도록 main 에 발행
    if learned_pub:
        log(f"학습 계수 {len(learned_pub)}개를 learned.json 에 남겼습니다 (바뀐 게 있을 때만 커밋됩니다).")

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
