#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 키 로테이션(KeyPool + fetch_prices) 검증 — 네트워크 없이."""
import importlib.util
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
NL = chr(10)

fails = []


def check(label, got, want):
    ok = got == want
    print(("  ✓ " if ok else "  ✗ ") + label + (f"   got={got!r} want={want!r}" if not ok else ""))
    if not ok:
        fails.append(label)


def fresh_module():
    spec = importlib.util.spec_from_file_location("watch_kp_%d" % len(fails), os.path.join(HERE, "watch.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.time = type("T", (), {"sleep": staticmethod(lambda s: None)})()  # 재시도 대기 제거
    return m


W = fresh_module()

print("[1] 키 문자열 파싱")
check("줄바꿈으로 5개", len(W.KeyPool(NL.join(["k1", "k2", "k3", "k4", "k5"])).keys), 5)
check("쉼표로 3개", len(W.KeyPool("a, b ,c").keys), 3)
check("빈 줄·공백 무시", len(W.KeyPool("  a " + NL + NL + "  b  " + NL).keys), 2)
check("한 개", len(W.KeyPool("solo").keys), 1)
check("빈 값은 0개", len(W.KeyPool("").keys), 0)
check("None 도 0개", len(W.KeyPool(None).keys), 0)

print("")
print("[2] 라운드로빈")
pool = W.KeyPool("k1 k2 k3")
check("순서대로 돌고 처음으로", [pool.take() for _ in range(4)], ["k1", "k2", "k3", "k1"])
check("키가 없으면 None", W.KeyPool("").take(), None)

print("")
print("[3] 429 → 다음 키로 넘어간다")
calls = []
OK_BODY = {"data": [{"kind_id": 1, "name": "x", "min_price": 10, "total_count": 5, "is_sold_out": False}]}


def resp_429_for_k1(url, **kw):
    key = kw["headers"]["Authorization"].split()[-1]
    calls.append(key)
    return (429, None) if key == "k1" else (200, OK_BODY)


W._request = resp_429_for_k1
pool = W.KeyPool("k1 k2")
status, data = W.fetch_prices(pool, "x")
check("k1 이 막히면 k2 로", calls, ["k1", "k2"])
check("결국 성공", status, 200)
check("데이터도 옴", bool(data), True)

print("")
print("[4] 401 은 그 키를 이번 실행 내내 제외한다")
calls.clear()


def resp_401_for_k1(url, **kw):
    key = kw["headers"]["Authorization"].split()[-1]
    calls.append(key)
    return (401, None) if key == "k1" else (200, {"data": []})


W._request = resp_401_for_k1
pool = W.KeyPool("k1 k2")
W.fetch_prices(pool, "x")
check("401 키가 dead 에 들어감", "k1" in pool.dead, True)
calls.clear()
W.fetch_prices(pool, "y")
check("dead 키는 다시 쓰지 않음", calls, ["k2"])

print("")
print("[5] 전부 죽으면 실패를 보고한다 (조용히 성공하지 않는다)")
W._request = lambda url, **kw: (401, None)
pool = W.KeyPool("k1 k2")
status, data = W.fetch_prices(pool, "x")
check("데이터 없음", data, None)
check("두 키 모두 dead", len(pool.dead), 2)

print("")
print("[6] 한 개짜리 키도 예전과 똑같이 동작한다 (회귀 방지)")
W._request = lambda url, **kw: (200, OK_BODY)
pool = W.KeyPool("only")
status, data = W.fetch_prices(pool, "x")
check("성공", status, 200)
check("데이터 1건", len(data), 1)

W._request = lambda url, **kw: (500, None)
pool = W.KeyPool("only")
status, data = W.fetch_prices(pool, "x")
check("500 은 재시도 없이 그대로 보고", (status, data), (500, None))

print("")
print("─" * 50)
print("❌ 실패 " + str(len(fails)) + "건: " + ", ".join(fails) if fails else "✅ 전부 통과")
sys.exit(1 if fails else 0)
