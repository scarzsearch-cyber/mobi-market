#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watch.py 의 판정 로직을 API 없이 검증한다."""
import importlib.util
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("watch", os.path.join(HERE, "watch.py"))
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

fails = []


def check(label, got, want):
    ok = got == want
    print(("  ✓ " if ok else "  ✗ ") + label + (f"   got={got!r} want={want!r}" if not ok else ""))
    if not ok:
        fails.append(label)


def snap(price=1000, count=10, sold_out=False):
    return {"kind_id": 1, "name": "테스트", "min_price": price,
            "total_count": count, "is_sold_out": sold_out}


print("\n[1] 첫 실행에는 아무것도 울리지 않는다 (비교 기준 없음)")
w = {"kind_id": 1, "name": "테스트", "listingDropAlert": True,
     "lowestPriceAlert": True, "highestPriceAlert": True}
msgs, st = watch.evaluate(snap(), {}, w, [])
check("첫 실행 알림 0건", len(msgs), 0)
check("상태에 가격 기록됨", st["price"], 1000)

print("\n[2] 품귀 — 임계값 미만은 안 울린다")
prev = {"price": 1000, "count": 10, "soldOut": False}
w3 = {"kind_id": 1, "name": "테스트", "listingDropAlert": True, "listingDropThreshold": 4}
msgs, _ = watch.evaluate(snap(count=6), prev, w3, [])
check("4개 감소(임계 4) → 품귀 울림", len(msgs), 1)
check("품귀 문구", "품귀" in (msgs[0] if msgs else ""), True)

print("\n[3] 저가하락 — 임계 %와 연속확인(streak)")
wl = {"kind_id": 1, "name": "테스트", "lowestPriceAlert": True,
      "lowestPriceThresholdPct": 5, "lowestPriceConfirm": 2}
msgs, st = watch.evaluate(snap(price=970), prev, wl, [])   # -3% → 임계 미달
check("-3%(임계 5%) → 안 울림", len(msgs), 0)
check("streak 안 쌓임", st["streak_low"], 0)
msgs, st = watch.evaluate(snap(price=900), prev, wl, [])   # -10%, 1회차
check("-10% 1회차 → 아직 안 울림(연속 2회 필요)", len(msgs), 0)
check("streak 1", st["streak_low"], 1)
msgs, st = watch.evaluate(snap(price=900), {**prev, "streak_low": 1}, wl, [])
check("-10% 2회차 → 울림", len(msgs), 1)
check("울린 뒤 streak 리셋", st["streak_low"], 0)

print("\n[4] 품절 ↔ 매물있음 은 가격 비교 대상이 아니다")
wl2 = {"kind_id": 1, "name": "테스트", "lowestPriceAlert": True, "highestPriceAlert": True}
msgs, _ = watch.evaluate(snap(sold_out=True), prev, wl2, [])
check("매물있음 → 품절: 가격 알림 0건", len(msgs), 0)
msgs, _ = watch.evaluate(snap(price=500), {"price": None, "count": 10, "soldOut": True}, wl2, [])
check("품절 → 매물있음: 가격 알림 0건", len(msgs), 0)

print("\n[5] 목표가(threshold) — 한 번 울리면 벗어나야 재무장")
al = [{"id": 7, "type": "threshold", "price": 1200, "dir": "above"}]
w5 = {"kind_id": 1, "name": "테스트"}
msgs, st = watch.evaluate(snap(price=1300), prev, w5, al)
check("1300 ≥ 1200 → 울림", len(msgs), 1)
check("fired 기록됨", st["fired"].get("7"), True)
msgs, st = watch.evaluate(snap(price=1350), {**prev, "fired": {"7": True}}, w5, al)
check("계속 조건 만족 → 재전송 안 함", len(msgs), 0)
msgs, st = watch.evaluate(snap(price=1100), {**prev, "fired": {"7": True}}, w5, al)
check("조건 벗어남 → 재무장", st["fired"].get("7"), None)
check("재무장 시점엔 안 울림", len(msgs), 0)
msgs, st = watch.evaluate(snap(price=1300), {**prev, "fired": {}}, w5, al)
check("재무장 후 다시 도달 → 울림", len(msgs), 1)

print("\n[6] 목표가 여유폭(marginPct) — 경계선 진동 억제")
alm = [{"id": 8, "type": "threshold", "price": 1000, "dir": "above", "marginPct": 10}]
msgs, st = watch.evaluate(snap(price=950), {**prev, "fired": {"8": True}}, w5, alm)
check("950 (-5%) → 아직 재무장 안 함", st["fired"].get("8"), True)
msgs, st = watch.evaluate(snap(price=890), {**prev, "fired": {"8": True}}, w5, alm)
check("890 (-11%) → 재무장", st["fired"].get("8"), None)

print("\n[8] 목표가가 아닌 옛 형식은 건너뛴다")
msgs, _ = watch.evaluate(snap(price=1), prev, w5, [{"id": 10, "type": "trend_reversal", "direction": "any"}])
check("추세전환 알림 0건", len(msgs), 0)

print("\n[9] 조용한 시간대 — 자정을 넘는 구간 포함")
from datetime import datetime
KST = watch.KST
q = {"enabled": True, "start": "23:00", "end": "08:00"}
check("23:30 → 조용함", watch.in_quiet_hours(q, datetime(2026, 9, 2, 23, 30, tzinfo=KST)), True)
check("02:00 → 조용함", watch.in_quiet_hours(q, datetime(2026, 9, 2, 2, 0, tzinfo=KST)), True)
check("07:59 → 조용함", watch.in_quiet_hours(q, datetime(2026, 9, 2, 7, 59, tzinfo=KST)), True)
check("08:00 → 안 조용함(경계)", watch.in_quiet_hours(q, datetime(2026, 9, 2, 8, 0, tzinfo=KST)), False)
check("15:00 → 안 조용함", watch.in_quiet_hours(q, datetime(2026, 9, 2, 15, 0, tzinfo=KST)), False)
q2 = {"enabled": True, "start": "09:00", "end": "18:00"}
check("낮 구간 12:00 → 조용함", watch.in_quiet_hours(q2, datetime(2026, 9, 2, 12, 0, tzinfo=KST)), True)
check("낮 구간 20:00 → 안 조용함", watch.in_quiet_hours(q2, datetime(2026, 9, 2, 20, 0, tzinfo=KST)), False)
check("꺼져 있으면 항상 False", watch.in_quiet_hours({"enabled": False, "start": "23:00", "end": "08:00"},
                                                     datetime(2026, 9, 2, 23, 30, tzinfo=KST)), False)
check("설정 없으면 False", watch.in_quiet_hours(None, datetime(2026, 9, 2, 23, 30, tzinfo=KST)), False)

print("\n[10] 시크릿이 없으면 실패가 아니라 '건너뜀'(종료코드 0)")
for k in ("MOBI_API_KEY", "KAKAO_REST_KEY", "KAKAO_REFRESH_TOKEN"):
    os.environ.pop(k, None)
check("main() 종료코드", watch.main(), 0)


# ── [11] 천장 기준 신호 (v2) ──
print("")
print("[11] 천장 기준 신호 — 평균 100 이면 천장 150")
w11 = {"kind_id": 1, "name": "테스트", "avgPrice": 100, "sellHighAlert": True, "sellHighPct": 90,
       "buyLowAlert": True, "buyLowPct": 60}
base = {"price": 100, "count": 10, "soldOut": False, "fired": {}}
msgs, st = watch.evaluate(snap(price=140), base, w11, [])       # 93%
check("93% → 천장근접 1건", len(msgs), 1)
check("문구에 천장 150", "천장(150)" in msgs[0], True)
check("sellHigh fired 기록", st["fired"].get("sellHigh"), True)
msgs, st = watch.evaluate(snap(price=145), {**base, "fired": {"sellHigh": True}}, w11, [])
check("계속 천장 근처면 재전송 안 함", len(msgs), 0)
msgs, st = watch.evaluate(snap(price=110), {**base, "fired": {"sellHigh": True}}, w11, [])  # 73% < 80
check("80% 아래로 내려오면 재무장", st["fired"].get("sellHigh"), None)
msgs, st = watch.evaluate(snap(price=80), base, w11, [])        # 53%
check("53% → 저가 1건", len(msgs), 1)
check("저가 문구", "살 때" in msgs[0], True)
msgs, st = watch.evaluate(snap(price=100), {**base, "fired": {"buyLow": True}}, w11, [])  # 67% < 70
check("70% 아래면 아직 재무장 안 함", st["fired"].get("buyLow"), True)
msgs, st = watch.evaluate(snap(price=110), {**base, "fired": {"buyLow": True}}, w11, [])  # 73% > 70
check("70% 넘으면 재무장", st["fired"].get("buyLow"), None)
msgs, st = watch.evaluate(snap(price=120), base, w11, [])       # 80% 중간
check("중간(80%)이면 아무것도 안 울림", len(msgs), 0)
msgs, st = watch.evaluate(snap(price=140), base, {**w11, "avgPrice": None}, [])
check("평균가 없으면 천장 신호 없음", len(msgs), 0)
msgs, st = watch.evaluate(snap(price=140, sold_out=True), base, w11, [])
check("품절이면 천장 신호 없음", len(msgs), 0)

print("")
print("[12] 되사기 — 판매가 100 이면 85 아래가 남는 구간")
w12 = {"kind_id": 1, "name": "테스트", "rebuyAlert": True, "mySellPrice": 100}
msgs, st = watch.evaluate(snap(price=80), base, w12, [])
check("80 → 되사기 1건 (+5)", len(msgs), 1)
check("이득 +5 표기", "+5" in msgs[0], True)
msgs, st = watch.evaluate(snap(price=85), base, w12, [])
check("85 = 손익분기 → 안 울림", len(msgs), 0)
msgs, st = watch.evaluate(snap(price=90), {**base, "fired": {"rebuy": True}}, w12, [])
check("다시 올라가면 재무장", st["fired"].get("rebuy"), None)
msgs, st = watch.evaluate(snap(price=80), base, {**w12, "mySellPrice": None}, [])
check("판매가 없으면 되사기 없음", len(msgs), 0)


# ── [13] 평균가 추정기 + 우선순위 (index.html effectiveAvg 와 같은 규약, 같은 픽스처) ──
print("")
print("[13] 평균가 추정 — 72h 거래량 가중, 6h 관측 하한, 우선순위")
from datetime import datetime, timezone, timedelta
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()
def pt(h_ago, close, high, c_open, c_close):
    return {"time": (NOW - timedelta(hours=h_ago)).isoformat().replace("+00:00", "Z"),
            "close": close, "high": high, "count_open": c_open, "count_close": c_close}
# 8시간치. 팔린 수량 = count_open − count_close → 110 에서 20개, 130 에서 40개 → 가중평균 123.33
# 최근 6h 창은 >= 라 정확히 6h 전 캔들도 포함된다. 그래서 창 밖 검사는 8h·7h 전에 둔다.
# 창 안(5h..0h) max(high) = 150 → 하한 100. 8h·7h 전의 high 200 은 창 밖이라 무시돼야 한다.
PTS = [pt(8, 100, 200, 50, 50), pt(7, 100, 200, 50, 50),
       pt(5, 110, 115, 60, 50), pt(4, 110, 125, 60, 50),
       pt(3, 120, 125, 50, 50), pt(2, 120, 135, 50, 50),
       pt(1, 130, 135, 70, 50), pt(0, 130, 150, 70, 50)]
est, floor_avg, _pl, n = watch.estimate_avg(PTS, NOW_TS)
check("표본 8", n, 8)
check("거래량 가중 평균 123.33", round(est, 2), 123.33)
check("6h 관측 하한 150/1.5=100 (8h·7h 전 200 은 창 밖)", round(floor_avg, 2), 100.0)
ZERO = [pt(5, 100, 100, 9, 9), pt(4, 110, 110, 9, 9), pt(3, 120, 120, 9, 9),
        pt(2, 130, 130, 9, 9), pt(1, 140, 140, 9, 9), pt(0, 150, 150, 9, 9)]
est2, _, _, n2 = watch.estimate_avg(ZERO, NOW_TS)
check("팔린 수량 0 이면 단순 평균으로 (6개 → 125)", (n2, round(est2, 2)), (6, 125.0))
check("표본 5개 이하면 None", watch.estimate_avg(PTS[:5], NOW_TS)[0], None)
check("8캔들 픽스처엔 plateau 없음 (max(high) 150 이 1캔들)", _pl, None)

ms = lambda h_ago: int((NOW_TS - h_ago * 3600) * 1000)
a, src, rc = watch.effective_avg({"avgPrice": 150, "avgPriceAt": ms(1)}, est, floor_avg, None, NOW_TS)
check("1h 전 입력 150 → 입력", (a, src, rc), (150, "입력", False))
a, src, rc = watch.effective_avg({"avgPrice": 90, "avgPriceAt": ms(1)}, est, floor_avg, None, NOW_TS)
check("입력 90 < 하한 100 → 하한으로 올리고 재보정", (a, src, rc), (100.0, "입력·하한", True))
a, src, rc = watch.effective_avg({"avgPrice": 150, "avgPriceAt": ms(13), "avgRatio": 0.9, "avgRatioAt": ms(24)}, est, floor_avg, None, NOW_TS)
check("13h 전 입력은 낡음 → 추정×0.9 = 111 (보정)", (round(a, 2), src, rc), (111.0, "보정", False))
a, src, rc = watch.effective_avg({"avgRatio": 0.9, "avgRatioAt": ms(24 * 8)}, est, floor_avg, None, NOW_TS)
check("계수가 오래돼도 증거 없인 재촉 안 함", (round(a, 2), src, rc), (111.0, "보정", False))
a, src, rc = watch.effective_avg({}, est, floor_avg, None, NOW_TS)
check("아무것도 없으면 미보정 추정", (round(a, 2), src, rc), (123.33, "미보정", True))
a, src, rc = watch.effective_avg({"avgPrice": 140, "avgPriceAt": ms(30)}, None, None, None, NOW_TS)
check("추정 불가 + 옛 입력 → 옛입력", (a, src, rc), (140, "옛입력", True))
a, src, rc = watch.effective_avg({}, None, None, None, NOW_TS)
check("아무것도 없으면 None", a, None)

# evaluate 는 avgPrice_effective 를 우선한다
w13 = {"kind_id": 1, "name": "테스트", "avgPrice": 999, "avgPrice_effective": 100, "sellHighAlert": True}
msgs, st = watch.evaluate(snap(price=140), {"price": 100, "count": 10, "soldOut": False, "fired": {}}, w13, [])
check("유효 평균 100 → 천장 150, 140은 93% → 울림", len(msgs), 1)


# ── [14] plateau 자동보정 (index.html 과 같은 픽스처) ──
print("")
print("[14] plateau 자동보정 — 물량이 마른 시간대에 최저가가 천장에 붙는다")
def ptc(h_ago, close, high, cnt):
    return {"time": (NOW - timedelta(hours=h_ago)).isoformat().replace("+00:00", "Z"),
            "close": close, "high": high, "count_open": cnt, "count_close": cnt}
# 12캔들, 종가 100. 최근 3캔들(h 2,1,0) high 150 · 수량 30 (평소 100 의 30%) → 천장 150 → 평균 100
base12 = [ptc(h, 100, 105, 100) for h in range(11, 3, -1)]        # h 11..4 (8개)
A = base12 + [ptc(3, 100, 105, 100), ptc(2, 100, 150, 30), ptc(1, 100, 150, 30), ptc(0, 100, 150, 30)]
est, fl, pl, n = watch.estimate_avg(A, NOW_TS)
check("A: 추정 100 (팔린 수량 0 → 단순 평균)", round(est, 2), 100.0)
check("A: plateau 3캔들·수량 30% → 자동보정 100", pl, 100.0)
check("A: 하한 150/1.5 = 100", round(fl, 2), 100.0)
B = base12 + [ptc(3, 100, 105, 100), ptc(2, 100, 150, 100), ptc(1, 100, 150, 100), ptc(0, 100, 150, 100)]
check("B: plateau 수량이 평소와 같으면(가짜) 기각", watch.estimate_avg(B, NOW_TS)[2], None)
C = base12 + [ptc(3, 100, 105, 100), ptc(2, 100, 300, 30), ptc(1, 100, 300, 30), ptc(0, 100, 300, 30)]
check("C: 역산 200 이 추정 100 의 2배 → 대역 밖, 기각", watch.estimate_avg(C, NOW_TS)[2], None)
D = base12 + [ptc(3, 100, 105, 100), ptc(2, 100, 105, 100), ptc(1, 100, 150, 30), ptc(0, 100, 150, 30)]
check("D: 2캔들뿐이면 기각", watch.estimate_avg(D, NOW_TS)[2], None)

a, src, rc = watch.effective_avg({}, est, fl, pl, NOW_TS)
check("plateau 있으면 자동 (재촉 없음)", (a, src, rc), (100.0, "자동", False))
a, src, rc = watch.effective_avg({"avgRatio": 0.5, "avgRatioAt": ms(1)}, est, fl, pl, NOW_TS)
check("plateau 가 계수보다 우선", (a, src), (100.0, "자동"))
a, src, rc = watch.effective_avg({"avgPrice": 120, "avgPriceAt": ms(1)}, est, fl, pl, NOW_TS)
check("12h 안 입력이 plateau 보다 우선", (a, src), (120, "입력"))
a, src, rc = watch.effective_avg({"avgRatio": 0.9, "avgRatioAt": ms(48)}, est, fl, None, NOW_TS,
                                 learned={"ratio": 1.2, "ratioAt": NOW_TS - 3600})
check("설정 계수(2일 전)와 서버 학습 계수(1h 전) 중 더 최근 것", (round(a, 1), src), (120.0, "보정"))
# 하한(100)이 90 을 덮으므로, 계수 선택만 보려고 하한을 뺀다
a, src, rc = watch.effective_avg({"avgRatio": 0.9, "avgRatioAt": ms(1)}, est, None, None, NOW_TS,
                                 learned={"ratio": 1.2, "ratioAt": NOW_TS - 48 * 3600})
check("반대로 설정 계수가 더 최근이면 그것", (round(a, 1), src), (90.0, "보정"))
a, src, rc = watch.effective_avg({"avgRatio": 0.9, "avgRatioAt": ms(1)}, est, fl, None, NOW_TS,
                                 learned={"ratio": 1.2, "ratioAt": NOW_TS - 48 * 3600})
check("같은 경우 하한 100 이 있으면 끌어올리고 ⏰", (round(a, 1), src, rc), (100.0, "보정·하한", True))

print("")
print("[15] 토글 기본 켜짐 — 키가 없으면 켜진 것으로")
w15 = {"kind_id": 1, "name": "테스트", "avgPrice_effective": 100}
msgs, st = watch.evaluate(snap(price=140), {"price": 100, "count": 10, "soldOut": False, "fired": {}}, w15, [])
check("sellHighAlert 키 없음 → 93% 에 울림", len(msgs), 1)
msgs, st = watch.evaluate(snap(price=140), {"price": 100, "count": 10, "soldOut": False, "fired": {}}, {**w15, "sellHighAlert": False}, [])
check("명시적 False 면 안 울림", len(msgs), 0)

print("\n" + ("─" * 50))
print("❌ 실패 " + str(len(fails)) + "건: " + ", ".join(fails) if fails else "✅ 전부 통과")
sys.exit(1 if fails else 0)
