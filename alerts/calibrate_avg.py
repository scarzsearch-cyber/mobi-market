#!/usr/bin/env python3
"""게임의 '평균 판매가'(3일 거래 평균, 12시간 갱신)를 API 시세이력으로 재현할 수 있는지 검증.

API 는 평균가도 판매가 상한도 주지 않는다. 상한 = 평균 x 1.5 는 소유자 실측 4건으로
확인됐으므로, 평균만 추정할 수 있으면 상한을 화면에 자동으로 띄울 수 있다.
여러 추정법을 소유자가 게임에서 읽은 정답과 대조해 비율을 찍는다. 값이 아니라
'어느 방법이 맞는가'를 고르는 것이 목적이다.

소유자 실측 (2026-09-02 새벽 KST 기준):
  파동의 영혼석   평균 31      상한 47
  삼림의 영혼석   평균 22      상한 33
  공명의 영혼석   평균 45      상한 68
  마력 깃든 용비늘 평균 289346  상한 434019
"""
import json
import os
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://open.mabimobi.life/v1"
KST = timezone(timedelta(hours=9))

TRUTH = [
    ("파동의 영혼석",   281480597107872, 31.0,     47.0),
    ("삼림의 영혼석",   281481249153703, 22.0,     33.0),
    ("공명의 영혼석",   281480871837308, 45.0,     68.0),
    ("마력 깃든 용비늘", 281481283031319, 289346.0, 434019.0),
]


def fetch(url, key):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def boundary_before(t, hours_kst):
    """t 이전의 가장 최근 KST 정각(hours_kst 중 하나)."""
    tk = t.astimezone(KST)
    cands = []
    for d in (0, 1):
        day = (tk - timedelta(days=d)).date()
        for h in hours_kst:
            b = datetime(day.year, day.month, day.day, h, tzinfo=KST)
            if b <= tk:
                cands.append(b)
    return max(cands)


def estimates(points, t_end):
    t_start = t_end - timedelta(hours=72)
    win = [p for p in points if p.get("close") is not None and t_start <= parse_ts(p["time"]) <= t_end]
    if len(win) < 3:
        return None, len(win)
    closes = [p["close"] for p in win]
    lows = [p["low"] for p in win if p.get("low") is not None]
    ohlc4 = [(p["open"] + p["high"] + p["low"] + p["close"]) / 4 for p in win
             if all(p.get(k) is not None for k in ("open", "high", "low", "close"))]
    sc = [p.get("sample_count") or 1 for p in win]
    wmean = sum(c * w for c, w in zip(closes, sc)) / sum(sc)
    sold = [max(0, (p.get("count_open") or 0) - (p.get("count_close") or 0)) for p in win]
    sold_w = (sum(c * q for c, q in zip(closes, sold)) / sum(sold)) if sum(sold) > 0 else None
    return {
        "close_mean": statistics.mean(closes),
        "close_median": statistics.median(closes),
        "close_wmean(sample_count)": wmean,
        "ohlc4_mean": statistics.mean(ohlc4) if ohlc4 else None,
        "low_mean": statistics.mean(lows) if lows else None,
        "sold_weighted": sold_w,
    }, len(win)


def main():
    key = os.environ.get("MOBI_API_KEY", "").strip().split()[0] if os.environ.get("MOBI_API_KEY", "").strip() else ""
    if not key:
        print("MOBI_API_KEY 없음")
        return 1
    now = datetime.now(timezone.utc)
    windows = [
        ("지금",           now),
        ("직전 06시 KST",  boundary_before(now, (6,))),
        ("직전 06/18시",   boundary_before(now, (6, 18))),
        ("직전 00/12시",   boundary_before(now, (0, 12))),
    ]
    print("실행 시각:", now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST"))
    print()
    summary = {}
    for name, kid, truth_avg, truth_cap in TRUTH:
        data = fetch(f"{BASE}/market/prices/history?kind_id={kid}&days=7", key)
        pts = data.get("points") or []
        pts.sort(key=lambda p: p["time"])
        nn = [p for p in pts if p.get("close") is not None]
        has_count = sum(1 for p in pts if p.get("count_open") is not None)
        print("=" * 72)
        print(name, "| resolution:", data.get("resolution"), "| 캔들", len(pts), "| close 있음", len(nn),
              "| count_* 있음", has_count)
        if nn:
            print("   범위:", nn[0]["time"], "~", nn[-1]["time"])
        print(f"   정답: 평균 {truth_avg:g}  상한 {truth_cap:g}  (상한/평균 = {truth_cap/truth_avg:.3f})")
        for wname, t_end in windows:
            est, n = estimates(pts, t_end)
            if not est:
                print(f"   [{wname}] 창 안 캔들 {n}개 — 부족")
                continue
            print(f"   [{wname} 기준 72h, 캔들 {n}개]")
            for k, v in est.items():
                if v is None:
                    print(f"      {k:26} —")
                    continue
                ratio = v / truth_avg
                mark = " <<<" if abs(ratio - 1) < 0.05 else ""
                print(f"      {k:26} {v:>12.2f}   /정답 = {ratio:.3f}{mark}")
                summary.setdefault((wname, k), []).append(ratio)
    print()
    print("=" * 72)
    print("추정법별 요약 (4개 아이템의 정답 대비 비율: 평균 / 최대편차)")
    rows = []
    for (wname, k), rs in summary.items():
        if len(rs) < 4:
            continue
        m = statistics.mean(rs)
        dev = max(abs(r - 1) for r in rs)
        rows.append((dev, wname, k, m, rs))
    rows.sort()
    for dev, wname, k, m, rs in rows:
        print(f"  최대편차 {dev:6.1%} | 평균비 {m:.3f} | {wname:14} {k:26} | " + " ".join(f"{r:.3f}" for r in rs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
