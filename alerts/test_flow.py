#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watch.py 의 main() 전체 흐름을 네트워크 없이 돌려본다."""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "watch.py")

fails = []


def check(label, got, want):
    ok = got == want
    print(("  ✓ " if ok else "  ✗ ") + label + (f"   got={got!r} want={want!r}" if not ok else ""))
    if not ok:
        fails.append(label)


def fresh(tmp, price=1000, count=10):
    """watch.py 를 임시 폴더 기준으로 새로 로드하고, HTTP 를 가짜로 갈아끼운다."""
    spec = importlib.util.spec_from_file_location("watch_%d" % id(tmp), SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.HERE = tmp
    m.SETTINGS_PATH = os.path.join(tmp, "settings.json")
    m.STATE_PATH = os.path.join(tmp, "state.json")
    m.REQUEST_GAP_SEC = 0  # 테스트에서 기다리지 않는다

    sent = []
    m.fetch_prices = lambda k, n: (200, [{"kind_id": 1, "name": "파동의 영혼석",
                                          "min_price": price, "total_count": count,
                                          "is_sold_out": False}])
    m.kakao_access_token = lambda r, t: ("fake-access", None, None)

    def fake_send(access, text, link):
        sent.append((text, link))
        return True, 200
    m.kakao_send = fake_send
    return m, sent


SETTINGS = {
    "version": 1,
    "watchList": [{"kind_id": 1, "name": "파동의 영혼석",
                   "lowestPriceAlert": True, "lowestPriceThresholdPct": 5,
                   "highestPriceAlert": True, "highestPriceThresholdPct": 5}],
    "alertsByKind": {"1": [{"id": 3, "type": "threshold", "price": 1500, "dir": "above"}]},
    "quiet": {"enabled": False, "start": "23:00", "end": "08:00"},
    "kakaoItemCooldownMin": 30,
}

os.environ.update(MOBI_API_KEY="x", KAKAO_REST_KEY="y", KAKAO_REFRESH_TOKEN="z")
tmp = tempfile.mkdtemp()
try:
    with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(SETTINGS, f, ensure_ascii=False)

    print("\n[1] 첫 실행 — 비교 기준이 없으니 아무것도 안 보낸다")
    m, sent = fresh(tmp, price=1000)
    check("종료코드", m.main(), 0)
    check("전송 0건", len(sent), 0)
    st = json.load(open(os.path.join(tmp, "state.json"), encoding="utf-8"))
    check("상태 파일 생성됨", st["items"]["1"]["price"], 1000)
    check("lastRunAt 기록", "lastRunAt" in st, True)

    print("\n[2] 두 번째 실행 — 가격 -20% → 저가하락 전송")
    m, sent = fresh(tmp, price=800)
    check("종료코드", m.main(), 0)
    check("전송 1건", len(sent), 1)
    check("문구에 저가하락", "⬇" in sent[0][0], True)
    check("링크에 ?open=1", sent[0][1].endswith("?open=1"), True)

    print("\n[3] 곧바로 또 떨어져도 쿨다운(30분) 중이면 안 보낸다")
    m, sent = fresh(tmp, price=600)
    check("종료코드", m.main(), 0)
    check("전송 0건 (쿨다운)", len(sent), 0)
    st = json.load(open(os.path.join(tmp, "state.json"), encoding="utf-8"))
    check("상태는 계속 갱신됨", st["items"]["1"]["price"], 600)

    print("\n[4] 조용한 시간대면 카톡을 억제하되 상태는 갱신한다")
    s2 = dict(SETTINGS)
    s2["quiet"] = {"enabled": True, "start": "00:00", "end": "23:59"}
    s2["kakaoItemCooldownMin"] = 0
    with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(s2, f, ensure_ascii=False)
    m, sent = fresh(tmp, price=300)
    check("종료코드", m.main(), 0)
    check("전송 0건 (조용한 시간대)", len(sent), 0)
    st = json.load(open(os.path.join(tmp, "state.json"), encoding="utf-8"))
    check("상태 갱신됨", st["items"]["1"]["price"], 300)

    print("\n[5] 목표가 도달 — 쿨다운 0, 조용한 시간대 해제")
    s2["quiet"] = {"enabled": False, "start": "23:00", "end": "08:00"}
    with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(s2, f, ensure_ascii=False)
    m, sent = fresh(tmp, price=1600)
    check("종료코드", m.main(), 0)
    check("전송 1건", len(sent), 1)
    check("목표가 문구", "💰" in sent[0][0], True)
    check("저가상승도 같이", "⬆" in sent[0][0], True)

    print("\n[6] 조회가 전부 실패하면 실패(1)로 올려 메일이 오게 한다")
    m, sent = fresh(tmp)
    m.fetch_prices = lambda k, n: (401, None)
    check("종료코드 1", m.main(), 1)

    print("\n[7] settings.json 이 없으면 실패가 아니라 건너뜀")
    os.remove(os.path.join(tmp, "settings.json"))
    m, sent = fresh(tmp)
    check("종료코드 0", m.main(), 0)

    print("\n[8] 카카오 토큰 갱신 실패는 진짜 실패(1)")
    with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(s2, f, ensure_ascii=False)
    m, sent = fresh(tmp, price=100)
    m.kakao_access_token = lambda r, t: (None, None, "카카오 토큰 갱신 실패 (HTTP 401)")
    check("종료코드 1", m.main(), 1)

    print("\n[9] 로그에 시크릿이 새지 않는가")
    import contextlib
    with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(s2, f, ensure_ascii=False)
    os.environ.update(MOBI_API_KEY="SECRET-MOBI-123", KAKAO_REST_KEY="SECRET-REST-456",
                      KAKAO_REFRESH_TOKEN="SECRET-REFRESH-789")
    m, sent = fresh(tmp, price=2000)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.main()
    out = buf.getvalue()
    for name, val in (("MOBI", "SECRET-MOBI-123"), ("REST", "SECRET-REST-456"),
                      ("REFRESH", "SECRET-REFRESH-789")):
        check(f"{name} 키가 로그에 없음", val in out, False)
    check("상태 파일에도 없음", any(v in open(os.path.join(tmp, "state.json"), encoding="utf-8").read()
                                    for v in ("SECRET-MOBI-123", "SECRET-REST-456", "SECRET-REFRESH-789")), False)
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# ── [11] kind_id 없이 이름만으로 해석되는가 (손으로 적은 settings.json 경로) ──
print("\n[11] kind_id 없이 이름만 적어도 동작하는가")
tmp2 = tempfile.mkdtemp()
try:
    S = {"version": 1,
         "watchList": [{"name": "파동의 영혼석", "lowestPriceAlert": True, "lowestPriceThresholdPct": 3}],
         "alertsByKind": {}, "quiet": {"enabled": False, "start": "23:00", "end": "08:00"},
         "kakaoItemCooldownMin": 0}
    with open(os.path.join(tmp2, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(S, f, ensure_ascii=False)
    os.environ.update(MOBI_API_KEY="x", KAKAO_REST_KEY="y", KAKAO_REFRESH_TOKEN="z")

    def load(price, names):
        m, sent = fresh(tmp2, price=price)
        m.fetch_prices = lambda k, n: (200, [{"kind_id": 100 + i, "name": nm, "min_price": price,
                                              "total_count": 50, "is_sold_out": False}
                                             for i, nm in enumerate(names)])
        return m, sent

    m, sent = load(1000, ["파동의 영혼석", "야생의 영혼석"])
    check("이름 일치로 해석 → 종료 0", m.main(), 0)
    st = json.load(open(os.path.join(tmp2, "state.json"), encoding="utf-8"))
    check("해석된 kind_id 로 상태 저장", list(st["items"].keys()), ["100"])

    m, sent = load(700, ["파동의 영혼석", "야생의 영혼석"])
    check("두 번째 실행 -30% → 전송 1건", (m.main(), len(sent))[1], 1)

    # 같은 이름이 둘이면 임의로 고르지 않는다
    m, sent = load(1000, ["파동의 영혼석", "파동의 영혼석"])
    m.main()
    check("동명이인 → 전송 0건(건너뜀)", len(sent), 0)

    # 이름이 아예 없으면 건너뛴다
    m, sent = load(1000, ["엉뚱한 아이템"])
    m.main()
    check("이름 불일치 → 전송 0건", len(sent), 0)

    # kind_id 를 적어두면 그쪽이 우선
    S["watchList"][0]["kind_id"] = 101
    with open(os.path.join(tmp2, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(S, f, ensure_ascii=False)
    m, sent = load(1000, ["파동의 영혼석", "야생의 영혼석"])
    m.main()
    st = json.load(open(os.path.join(tmp2, "state.json"), encoding="utf-8"))
    check("kind_id 명시 시 그것을 사용", "101" in st["items"], True)
finally:
    shutil.rmtree(tmp2, ignore_errors=True)

# ── [12] 시크릿이 없어도 설정은 읽고 보고하는가 ──
print("\n[12] 시크릿 전에도 설정 파일 검증이 되는가")
tmp3 = tempfile.mkdtemp()
try:
    with open(os.path.join(tmp3, "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "watchList": [{"name": "파동의 영혼석"}]}, f, ensure_ascii=False)
    for k in ("MOBI_API_KEY", "KAKAO_REST_KEY", "KAKAO_REFRESH_TOKEN"):
        os.environ.pop(k, None)
    m, _ = fresh(tmp3)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = m.main()
    out = buf.getvalue()
    check("종료코드 0", code, 0)
    check("설정을 읽었다고 보고", "설정을 읽었습니다" in out, True)
    check("아이템 이름이 로그에 나옴", "파동의 영혼석" in out, True)
    check("시크릿 안내도 나옴", "시크릿이 아직" in out, True)
finally:
    shutil.rmtree(tmp3, ignore_errors=True)

print("\n" + "─" * 50)
print("❌ 실패 " + str(len(fails)) + "건: " + ", ".join(fails) if fails else "✅ 전부 통과")
sys.exit(1 if fails else 0)
