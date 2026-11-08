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
PATH2 = os.path.join("models", "ai3_v2.pkl")  # 2026-11-08 1着→2着→3着を順に出すAI(的中率+3〜4pt)
_M = None
COMBOS = list(itertools.permutations(range(6), 3))
_NUM = ["wr", "top2", "local", "motor", "boat", "ex", "avgst", "pvst", "tilt", "age", "weight"]
_RANK = {"wr": False, "top2": False, "local": False, "motor": False, "boat": False, "ex": True, "avgst": True, "pvst": True}
CLS = {"A1": 1, "A2": 2, "B1": 3, "B2": 4}


def features(rows: list[dict], stadium: int, race_number: int, wind=None, wave=None) -> list[list[float]] | None:
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
    return out


def load():
    global _M
    if _M is None:
        path = PATH2 if os.path.exists(PATH2) else PATH
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


def pick(rows, stadium, race_number, wind=None, wave=None, n=6, odds=None):
    """([(買い目, 確率)…上位n点], 級[, 穴(買い目, 確率) or None]) を返す。odds={"1-2-3":倍率}を渡すと穴も選ぶ。級: "S"=自信 上位2% / "A"=上位5% / None=買わない。"""
    f = features(rows, stadium, race_number, wind, wave)
    if f is None:
        return None
    p = trifecta_probs(f)
    allp = sorted(p.items(), key=lambda x: -x[1])
    top = allp[:n]
    v = top[0][1]
    m = load()
    tier = "S" if v >= m["q98"] else ("A" if v >= m["q95"] else None)
    if odds is not None:
        # 2026-11-08 ご主人様「鉄板6穴1の7点」: 鉄板の外から、オッズ50〜100倍で一番来そうな1点を穴として足す
        ana = next(((c, q) for c, q in allp[n:] if 50 <= (odds.get(c) or 0) < 100), None)
        return top, tier, ana
    return top, tier
