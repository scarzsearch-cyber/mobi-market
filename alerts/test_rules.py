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
w = {"kind_id": 1, "name": "테스트", "newListingAlert": True, "listingDropAlert": True,
     "lowestPriceAlert": True, "highestPriceAlert": True}
msgs, st = watch.evaluate(snap(), {}, w, [])
check("첫 실행 알림 0건", len(msgs), 0)
check("상태에 가격 기록됨", st["price"], 1000)

print("\n[2] 신규매물 / 품귀 — 임계값 미만은 안 울린다")
prev = {"price": 1000, "count": 10, "soldOut": False}
w2 = {"kind_id": 1, "name": "테스트", "newListingAlert": True, "newListingThreshold": 3}
msgs, _ = watch.evaluate(snap(count=12), prev, w2, [])
check("2개 증가(임계 3) → 안 울림", len(msgs), 0)
msgs, _ = watch.evaluate(snap(count=13), prev, w2, [])
check("3개 증가(임계 3) → 울림", len(msgs), 1)
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

print("\n[7] 트레일링 — 새 고점마다 재무장")
alt = [{"id": 9, "type": "trailing", "pct": 10}]
msgs, st = watch.evaluate(snap(price=1000), {}, w5, alt)
check("첫 관측 → 고점 기록만", st["peak"], 1000)
check("첫 관측엔 안 울림", len(msgs), 0)
msgs, st = watch.evaluate(snap(price=1200), {"peak": 1000}, w5, alt)
check("신고점 → 고점 갱신", st["peak"], 1200)
msgs, st = watch.evaluate(snap(price=1140), {"peak": 1200}, w5, alt)
check("고점 대비 -5% → 안 울림", len(msgs), 0)
msgs, st = watch.evaluate(snap(price=1050), {"peak": 1200}, w5, alt)
check("고점 대비 -12.5% → 울림", len(msgs), 1)
check("트레일링 fired 기록", st["fired"].get("9"), True)
msgs, st = watch.evaluate(snap(price=1300), {"peak": 1200, "fired": {"9": True}}, w5, alt)
check("새 고점 → 재무장", st["fired"].get("9"), None)

print("\n[8] trend_reversal 은 서버에서 판정하지 않는다 (화면 전용)")
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

print("\n" + ("─" * 50))
print("❌ 실패 " + str(len(fails)) + "건: " + ", ".join(fails) if fails else "✅ 전부 통과")
sys.exit(1 if fails else 0)
