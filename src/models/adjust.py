"""世間の見落とし補正AI(2026-10-26 ご主人様OK・実験枠②)。

3連単のオッズから出した「世間の確率」をもとに、世間が見落としている選手の特徴だけを足して確率を直す。
    log p(買い目) = b × log q(世間) + g・x(1着の艇) + d・x(2着の艇)   (レースごとに合計1に直す)
学習は 2025-06〜2026-06 の確定オッズ(約6.3万R)。係数は models/adjust_v1.json。
答えを見ていない 2026-07〜10月で、割安度(=補正確率×オッズ)0.9以上・100倍未満を買うと
22,807点・回収率90.5%(何も考えずに全部買うと77%)。
見つかった世間のクセ: A1・A2級を買いすぎ / 展示タイムが速い艇と、勝率の高い艇の2着を見落とし。
"""
from __future__ import annotations

import json
import math
import os

PATH = os.path.join("models", "adjust_v1.json")
_M = None
# 学習時(scratchpad m1.py)と同じ順番・同じ作り方
_NUM = ["wr", "local", "motor", "top2", "boat", "ex", "avgst", "pvst", "tilt", "age", "weight"]
_PAIR = ["wr", "local", "motor", "top2", "boat", "ex", "avgst", "pvst", "tilt", "age", "weight"]


def boat_features(rows: list[dict], wind=None, wave=None) -> list[list[float]] | None:
    """rows: 1〜6号艇の順で dict(wr=勝率, top2=2連率, local=当地勝率, motor=モーター2連率, boat=ボート2連率,
    ex=展示タイム, avgst=平均ST, pvst=展示ST(フライングはマイナス), tilt, age, weight, cls="A1"など, F=フライング数,
    course=展示の進入コース)。値がない所は None でよい(レース平均として扱う)。"""
    if len(rows) != 6:
        return None
    d = {}
    for k in _NUM:
        v = [r.get(k) for r in rows]
        ok = [x for x in v if x is not None]
        m = sum(ok) / len(ok) if ok else None
        filled = [x if x is not None else m for x in v]
        if m is None:
            d[k] = [0.0] * 6
            continue
        mm = sum(filled) / 6
        d[k] = [x - mm for x in filled]
    d["tilt"] = [max(-3.0, min(3.0, x)) for x in d["tilt"]]
    out = []
    for i, r in enumerate(rows):
        l1 = 1.0 if i == 0 else 0.0
        f = []
        for k in _PAIR:
            f += [d[k][i], d[k][i] * l1]
        cls = r.get("cls") or ""
        fl = 1.0 if (r.get("F") or 0) >= 1 else 0.0
        c = r.get("course")
        f += [float(cls == "A1"), float(cls == "A2"), float(cls == "B2"), fl, fl * l1,
              float(c is not None and c < i + 1), float(c is not None and c > i + 1),
              (wind or 0) * l1, (wave or 0) * l1]
        out.append(f)
    return out


def load():
    global _M
    if _M is None:
        with open(PATH, encoding="utf-8") as f:
            _M = json.load(f)
    return _M


def adjusted_probs(odds: dict[str, float], feats: list[list[float]]) -> dict[str, float]:
    """odds: {"1-2-3": 12.3, ...}  → 補正後の確率 {"1-2-3": p}"""
    m = load()
    mu, sd, b, g, dd = m["mu"], m["sd"], m["b"], m["g"], m["d"]
    z = [[(x - mu[j]) / sd[j] for j, x in enumerate(f)] for f in feats]
    sg = [sum(a * c for a, c in zip(zz, g)) for zz in z]
    s2 = [sum(a * c for a, c in zip(zz, dd)) for zz in z]
    inv = {k: 1.0 / o for k, o in odds.items() if o and o > 0}
    if not inv:
        return {}
    tot = sum(inv.values())
    v = {k: b * math.log(x / tot) + sg[int(k[0]) - 1] + s2[int(k[2]) - 1] for k, x in inv.items()}
    mx = max(v.values())
    e = {k: math.exp(x - mx) for k, x in v.items()}
    Z = sum(e.values())
    return {k: x / Z for k, x in e.items()}


def value_picks(odds: dict[str, float], feats: list[list[float]]) -> list[tuple[str, float, float]]:
    """割安度(補正確率×オッズ)がしきい値以上・上限オッズ未満の買い目 [(組番, オッズ, 割安度)] を割安な順に。"""
    m = load()
    p = adjusted_probs(odds, feats)
    out = [(k, o, p[k] * o) for k, o in odds.items() if k in p and o < m["max_odds"] and p[k] * o >= m["threshold"]]
    return sorted(out, key=lambda x: -x[2])
