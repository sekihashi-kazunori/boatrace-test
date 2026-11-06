"""単勝の売上(総額)を 6艇の単勝オッズの端数から推定する(2026-11-06)。

ボートの単勝オッズ = floor(0.75 × 総額 / その艇の票額, 0.1倍)、票額は100円単位。
6艇すべてのオッズと矛盾しない一番小さい総額を返す(過去データで96%のレースで求まる)。

リーサルウェポンの「売上に合わせて賭ける」(2026-11-06 ご主人様OK):
  S級 = 推定売上の3%、B級 = 1%(100円単位で切り捨て)。
  過去12か月(確定オッズ・自分の賭けでオッズが下がる分も計算)で 回収121%・月+3.8万。
"""
from __future__ import annotations

import math

import numpy as np

RATE = 0.75
STAKE_PCT = {"S": 0.03, "B": 0.01}


def estimate_pool(odds: dict, max_pool: int = 3_000_000) -> int | None:
    """odds: {"win_1": 3.4, ..., "win_6": 12.0}。求まらなければ None。"""
    try:
        o = np.array([float(odds[f"win_{i}"]) for i in range(1, 7)])
    except (KeyError, TypeError, ValueError):
        return None
    if not np.all(np.isfinite(o)) or np.any(o < 1):
        return None
    ps = np.arange(100, max_pool + 1, 100, dtype=np.int64)
    lo = np.floor(RATE * ps[:, None] / (100 * (o[None, :] + 0.1))) + 1
    hi = np.floor(RATE * ps[:, None] / (100 * o[None, :]) + 1e-9)
    ok = np.all(hi >= lo, 1) & (lo.sum(1) <= ps / 100) & (hi.sum(1) >= ps / 100)
    c = ps[ok]
    return int(c.min()) if len(c) else None


def stake(tier: str, pool: int | None) -> int:
    pct = STAKE_PCT.get(tier)
    if not pct or not pool:
        return 0
    return int(pct * pool // 100 * 100)


def odds_after(pool: int, win_1: float, bet: int) -> float:
    """自分が 1号艇の単勝に bet 円足したあとのオッズ。"""
    w1 = RATE * pool / win_1
    return math.floor(RATE * (pool + bet) / (w1 + bet) * 10 + 1e-9) / 10
