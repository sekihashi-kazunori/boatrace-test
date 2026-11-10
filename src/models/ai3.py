"""AI本予想(2026-11-08 ご主人様指示「1からやり直し・的中率を上げろ」)。

オッズを一切使わず、締切前に分かる情報だけで各艇の 1着・2着内・3着内 の確率を出し、
3連単120通りの確率に広げて、一番来そうな買い目を1点買う。
自信(一番の買い目の確率)が高いレースだけ買う。
学習 2024-06〜2025-12、答えを見ていない2026年1〜10月(4.7万R)で:
  自信 上位5%  的中率18.1%・平均配当492円・回収率89%
  自信 上位2%  的中率20.2%・平均配当456円・回収率92%
本番用のモデルは 2026-10 までの全データで学習し直したもの(models/ai3_v1.pkl)。
"""
from __future__ import annotations

import itertools
import os
import pickle

PATH = os.path.join("models", "ai3_v1.pkl")
PATH3 = os.path.join("models", "ai3_v3.pkl")  # 2026-11-09 場のクセ(直近1年)入り
PATH2 = os.path.join("models", "ai3_v2.pkl")  # 2026-11-08 1着→2着→3着を順に出すAI(的中率+3〜4pt)
_M = None
COMBOS = list(itertools.permutations(range(6), 3))
_NUM = ["wr", "top2", "local", "motor", "boat", "ex", "avgst", "pvst", "tilt", "age", "weight"]
_RANK = {"wr": False, "top2": False, "local": False, "motor": False, "boat": False, "ex": True, "avgst": True, "pvst": True}
CLS = {"A1": 1, "A2": 2, "B1": 3, "B2": 4}


def features(rows: list[dict], stadium: int, race_number: int, wind=None, wave=None,
             venue: dict | None = None) -> list[list[float]] | None:
    """rows: 1〜6号艇の順の dict(wr, top2, local, motor, boat, ex, avgst, pvst, tilt, age, weight,
    cls=1〜4 または "A1"など, F=フライング数, course=展示の進入コース)。無い値は None。"""
    if len(rows) != 6:
        return None
    nan = float("nan")
    val = {k: [(float(r[k]) if r.get(k) is not None else nan) for r in rows] for k in _NUM}
    for k in ("ex",):
        val[k] = [x if x == x and x > 0 else nan for x in val[k]]
    out = [[] for _ in range(6)]
    for k in _NUM:
        v = val[k]
        ok = [x for x in v if x == x]
        m = sum(ok) / len(ok) if ok else nan
        for i in range(6):
            out[i] += [v[i], (v[i] - m) if v[i] == v[i] and m == m else nan]
        if k in _RANK:
            asc = _RANK[k]
            for i in range(6):
                if v[i] != v[i]:
                    out[i].append(nan)
                else:
                    out[i].append(1.0 + sum(1 for x in ok if (x < v[i] if asc else x > v[i])))
    for i, r in enumerate(rows):
        c = r.get("cls")
        c = CLS.get(c, c) if isinstance(c, str) else c
        course = r.get("course")
        out[i] += [float(i + 1), float(course) if course is not None else nan, float(stadium), float(race_number),
                   float(wind) if wind is not None else nan, float(wave) if wave is not None else nan,
                   float(c) if c is not None else nan, float(r.get("F") or 0)]
    if venue is not None:
        # 2026-11-09 ご主人様「直近データが1番大切」: その場・そのコースの直近365日の 1着率・2連率・3連率
        sc = f"{int(stadium):02d}"
        in1 = venue.get((sc, 1), (nan, nan, nan))[0]
        for i, r in enumerate(rows):
            cs = r.get("course") or (i + 1)
            v = venue.get((sc, int(cs)), (nan, nan, nan))
            out[i] += [v[0], v[1], v[2], in1]
    return out


_V = {}


def _venue_cache(table):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    d = str(datetime.now(ZoneInfo("Asia/Tokyo")).date())
    if d not in _V:
        _V.clear()
        _V[d] = table(d)
    return _V[d]


def load():
    global _M
    if _M is None:
        path = next(p for p in (PATH3, PATH2, PATH) if os.path.exists(p))
        with open(path, "rb") as f:
            _M = pickle.load(f)
    return _M


def _seq_probs(m, feats) -> dict[str, float]:
    """2026-11-08 v2: 1着モデル × (1着がaのときの2着モデル) × (1・2着がa,bのときの3着モデル)。
    2026年1〜10月(答えを見ていない4.7万R)の自信上位5%で 6点的中率 56.2%→59.0%。"""
    import numpy as np
    X = np.array(feats, dtype=float)
    w = m["mw"].predict_proba(X)[:, 1]
    w = w / w.sum()
    pairs = list(itertools.permutations(range(6), 2))
    q2 = m["m2"].predict_proba(np.array([np.concatenate([X[b], X[a]]) for a, b in pairs]))[:, 1]
    Q2 = {}
    for a in range(6):
        idx = [i for i, (x, _y) in enumerate(pairs) if x == a]
        t = sum(q2[i] for i in idx)
        for i in idx:
            Q2[pairs[i]] = q2[i] / t
    q3 = m["m3"].predict_proba(np.array([np.concatenate([X[c], X[a], X[b]]) for a, b, c in COMBOS]))[:, 1]
    tot = {}
    for (a, b, _c), v in zip(COMBOS, q3):
        tot[(a, b)] = tot.get((a, b), 0) + v
    p = {f"{a + 1}-{b + 1}-{c + 1}": w[a] * Q2[(a, b)] * v / tot[(a, b)] for (a, b, c), v in zip(COMBOS, q3)}
    z = sum(p.values())
    return {k: v / z for k, v in p.items()}


def trifecta_probs(feats: list[list[float]]) -> dict[str, float]:
    m = load()
    if "mw" in m:
        return _seq_probs(m, feats)
    w = m["win"].predict_proba(feats)[:, 1]
    t2 = m["top2"].predict_proba(feats)[:, 1]
    t3 = m["top3"].predict_proba(feats)[:, 1]
    sw = sum(w)
    W = [x / sw for x in w]
    S2 = [max(b - a, 1e-4) for a, b in zip(w, t2)]
    S3 = [max(c - b, 1e-4) for b, c in zip(t2, t3)]
    p = {}
    for a, b, c in COMBOS:
        s2 = sum(S2) - S2[a]
        s3 = sum(S3) - S3[a] - S3[b]
        p[f"{a + 1}-{b + 1}-{c + 1}"] = W[a] * S2[b] / s2 * S3[c] / s3
    z = sum(p.values())
    return {k: v / z for k, v in p.items()}


def pick(rows, stadium, race_number, wind=None, wave=None, n=6, odds=None, venue=None, ladies=False):
    """([(買い目, 確率)…上位n点], 級[, 穴(買い目, 確率) or None]) を返す。odds={"1-2-3":倍率}を渡すと穴も選ぶ。級: "S"=自信 上位2% / "A"=上位5% / None=買わない。"""
    m0 = load()
    if m0.get("venue"):
        if venue is None:
            from src.tools.venue_stats import table
            venue = _venue_cache(table)
        f = features(rows, stadium, race_number, wind, wave, venue)
    else:
        f = features(rows, stadium, race_number, wind, wave)
    if f is None:
        return None
    p = trifecta_probs(f)
    allp = sorted(p.items(), key=lambda x: -x[1])
    top = allp[:n]
    v = top[0][1]
    m = load()
    # 2026-11-10 ご主人様「1日必ず10Rは出す」: 自信 上位10%(B・1日約14R・6点的中率57%)まで広げる
    # 2026-11-10 ご主人様「Bは無理にやらない。S・Aで10R」: 自信B(上位10%)をやめ、自信Aを上位7%(1日約10R・6点的中率57.5%)に
    # 2026-11-10 湊さんから学んだ「レースの選び方」: 一番の買い目の確率ではなく、上位6点の確率の合計で選ぶ。
    # 湊さんの227Rを執事が読むと6点的中67%(=選んだレースが当てやすい)。2026年で試すと 上位7%(1日約11R)の6点的中 57.5%→61.3%
    s6 = sum(q for _c, q in allp[:6])
    tier = "S" if s6 >= 0.5239 else ("A" if s6 >= 0.4819 else None)
    # 2026-11-10 ご主人様「レディースは的中しやすい。本予想に組み込め」: レディース戦は上位10%(0.468)まで自信Aにする
    # (レディース戦の上位10%の的中62.3% = その他の自信S・A 61.1%と同じくらい)
    if tier is None and ladies and s6 >= 0.468:
        tier = "A"
    if odds is not None:
        # 2026-11-08 ご主人様「鉄板6穴1の7点」: 鉄板の外から、オッズ50〜100倍で一番来そうな1点を穴として足す
        ana = None
        if int(stadium) == 8:
            # 2026-11-10 常滑の法則: 1号艇が2着の時の頭は4号艇のカドが多い(22.2%・全場18.3%)。常滑の穴は「4-1-x」を優先
            ana = next(((c, q) for c, q in allp[n:] if c.startswith("4-1-") and 30 <= (odds.get(c) or 0) < 150), None)
        ana = ana or next(((c, q) for c, q in allp[n:] if 50 <= (odds.get(c) or 0) < 100), None)
        return top, tier, ana
    return top, tier


def reasons(rows: list[dict], picks: list[str], wind=None) -> list[str]:
    """買い目に出てくる艇(1着候補・2着候補)の、レース前データでの強みを短く並べる(ご主人様指示: 中身を見て根拠のある予想)。"""
    def rank(k, low=False):
        v = {i: rows[i].get(k) for i in range(6)
             if rows[i].get(k) is not None and (k not in ("ex", "pvst") or rows[i].get(k) >= (0 if k == "pvst" else 0.01))}
        return {i: 1 + sum(1 for x in v.values() if (x < v[i] if low else x > v[i])) for i in v}
    R = {"勝率": rank("wr"), "当地": rank("local"), "モーター": rank("motor"), "展示": rank("ex", True),
         "展示ST": rank("pvst", True)}
    w1, w2 = {}, {}
    for c in picks:
        a, b = int(c[0]) - 1, int(c[2]) - 1
        w1[a] = w1.get(a, 0) + 1
        w2[b] = w2.get(b, 0) + 1
    boats = sorted(w1, key=lambda i: -w1[i])[:2]
    boats += [i for i in sorted(w2, key=lambda i: -w2[i]) if i not in boats][:2]
    out = []
    for i in boats:
        r = rows[i]
        good = [f"{k}{R[k][i]}位" for k in R if R[k].get(i, 9) <= 2]
        if R["展示ST"].get(i, 9) <= 2 and r.get("pvst") is not None:
            good[-1] = good[-1] + f"(.{int(round(abs(float(r['pvst'])) * 100)):02d})" if good[-1].startswith("展示ST") else good[-1]
        if r.get("pvst") is not None and float(r["pvst"]) < 0:
            good.append(f"展示でF(.{int(round(abs(float(r['pvst'])) * 100)):02d})")
        c = r.get("course")
        if c and int(c) != i + 1:
            good.append(f"進入{int(c)}コース")
        cls = r.get("cls")
        cls = {1: "A1", 2: "A2", 3: "B1", 4: "B2"}.get(cls, cls)
        role = "頭" if i in boats[:2] and w1.get(i, 0) >= 2 else "相手"
        out.append(f"{i + 1}号艇({cls or '-'}・{role}): " + ("・".join(good) if good else "目立つ数字なし"))
    if wind is not None:
        out.append(f"風{wind}m")
    return out



def allocate(picks: list[tuple[str, float]], odds: dict, total: int = 1500, ana: str | None = None,
             cap: int = 2000, ratio: float = 1.1) -> list[tuple[str, int]]:
    """2026-11-09 ご主人様指示「ガミを改善。安い買い目は外さない。ガミりそうなレースだけ2,000円まで」。
    1) まずは今まで通り、確率に合わせて total(1,500円)を配る(穴は100円)
    2) 当たっても投資の ratio(1.1)倍に届かない(=ガミりそうな)買い目があるレースだけ、
       その買い目に100円ずつ足していく。1レース cap(2,000円)まで"""
    tot = sum(p for c, p in picks if c != ana) or 1
    amt = {c: (100 if c == ana else max(100, int((total - (100 if ana else 0)) * p / tot / 100 + 0.5) * 100))
           for c, p in picks}
    prob = dict(picks)
    # 基本はちょうど total(1,500円)。100円単位の丸めのずれは、一番来そうな鉄板で調整する
    top = max((c for c in amt if c != ana), key=lambda x: prob.get(x, 0), default=None)
    if top:
        amt[top] = max(100, amt[top] + total - sum(amt.values()))
    # 「ガミりそうなレース」= 一番来そうな鉄板が当たっても投資の ratio 倍に届かないレース。それ以外は1,500円のまま
    if not top or not odds.get(top) or amt[top] * odds[top] >= ratio * sum(amt.values()):
        return [(c, amt[c]) for c, _p in picks]
    while True:
        inv = sum(amt.values())
        short = [c for c in amt if odds.get(c) and amt[c] * odds[c] < ratio * inv]
        if not short or inv + 100 > cap:
            break
        # 一番足りない(戻り÷投資が低い)買い目に100円足す
        c = min(short, key=lambda x: amt[x] * odds[x] / inv - prob.get(x, 0) * 1e-6)
        amt[c] += 100
    return [(c, amt[c]) for c, _p in picks]


def boat1_lose(rows, stadium, race_number, wind=None, wave=None) -> float | None:
    """執事の読みでの「1号艇が負ける確率」(荒れ度)。"""
    m = load()
    if "mw" not in m:
        return None
    venue = None
    if m.get("venue"):
        from src.tools.venue_stats import table
        venue = _venue_cache(table)
    f = features(rows, stadium, race_number, wind, wave, venue) if venue is not None else features(rows, stadium, race_number, wind, wave)
    if f is None:
        return None
    import numpy as np
    w = m["mw"].predict_proba(np.array(f, dtype=float))[:, 1]
    return float(1 - w[0] / w.sum())


def probs(rows, stadium, race_number, wind=None, wave=None) -> dict | None:
    """3連単120通りの確率(執事の読み)。"""
    m = load()
    venue = None
    if m.get("venue"):
        from src.tools.venue_stats import table
        venue = _venue_cache(table)
    f = features(rows, stadium, race_number, wind, wave, venue) if venue is not None else features(rows, stadium, race_number, wind, wave)
    return trifecta_probs(f) if f is not None else None
