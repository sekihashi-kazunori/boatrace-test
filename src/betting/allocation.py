from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations

import pandas as pd


@dataclass
class BetPlan:
    combination: str
    category: str
    stake: int
    predicted_prob: float
    odds: float | None = None
    reason: str = ""


def _harville_trifecta_probs(win_probs: dict[int, float]) -> dict[tuple[int, int, int], float]:
    lanes = list(win_probs.keys())
    result = {}
    for i, j, k in permutations(lanes, 3):
        p_i = win_probs[i]
        remaining_after_i = 1 - p_i
        if remaining_after_i <= 0:
            continue
        p_j_given_i = win_probs[j] / remaining_after_i
        remaining_after_ij = remaining_after_i - win_probs[j]
        if remaining_after_ij <= 0:
            continue
        p_k_given_ij = win_probs[k] / remaining_after_ij
        result[(i, j, k)] = p_i * p_j_given_i * p_k_given_ij
    return result


def _category_for_rank(prob_rank: int) -> str:
    """確率の高さの順位(0始まり)に基づく表示用カテゴリ。

    採用する買い目・金額配分は期待値(確率×オッズ)ベースだが、
    根拠文言での「本命/中穴/大穴」ラベルは確率の順位で付ける。
    (期待値が高くて選ばれても、確率自体は低い人気薄なら「大穴」
    のままにしたいため)
    """
    if prob_rank < 2:
        return "鉄板"
    if prob_rank < 5:
        return "中穴"
    return "大穴"


def _reason_for(combo: tuple[int, int, int], race_df: pd.DataFrame, category: str) -> str:
    first = race_df[race_df["lane_number"] == combo[0]].iloc[0]
    parts = [f"{combo[0]}号艇が軸"]
    if (first.get("course_base_win_rate") or 0) >= 0.5:
        parts.append("イン・逃げの信頼度が高い")
    if (first.get("exhibition_rank") or 99) <= 2:
        parts.append("展示タイム上位で足が良い")
    if (first.get("motor_2rate") or 0) >= 40:
        parts.append("モーター2連率が良い")
    if category == "大穴":
        parts.append("人気薄だが狙い目")
    elif category == "中穴":
        parts.append("2・3着争いの中穴")
    return " / ".join(parts)


def _ceil100(x: float) -> int:
    return int(-(-x // 100) * 100)


def build_bet_plan(
    race_df: pd.DataFrame,
    total_stake: int = 1000,
    odds_map: dict[tuple[int, int, int], float] | None = None,
    n_teppan: int = 4,
    n_chuuana: int = 2,
    n_ooana: int = 2,
    teppan_pool: int = 10,
    chuuana_range: tuple[float, float] = (15.0, 40.0),
    ooana_range: tuple[float, float] = (40.0, 100.0),
    **_ignored,
) -> list[BetPlan]:
    """
    レースの買い目と金額配分を決める(2026-10-10 ご主人様指定ルール)。

    合計8点・1000円:
      - 鉄板4点: 予測確率上位から選ぶ。どれが当たっても払戻が
        総額(1000円)以上になる金額を張る = ガミらない
      - 中穴2点: オッズ15〜40倍の中で予測確率の高い順に100円ずつ
      - 大穴2点: オッズ40〜100倍の中で予測確率の高い順に100円ずつ

    鉄板の予算は 1000 - 中穴・大穴の400 = 600円。
    鉄板1点に必要な金額は ceil(1000 / オッズ) を100円単位に切り上げ。
    確率上位 teppan_pool 点の中から、予算内に収まる4点を確率順に選ぶ。
    4点そろわない(=本命が低すぎてガミを避けられない)レースは見送り。
    余った予算は確率1位の鉄板に上乗せする。
    オッズが取得できないレースは見送り。
    """
    win_probs = dict(zip(race_df["lane_number"], race_df["predicted_win_prob"]))
    combo_probs = _harville_trifecta_probs(win_probs)
    prob_ranked = sorted(combo_probs.items(), key=lambda x: x[1], reverse=True)

    if not odds_map:
        print("オッズ未取得のため見送り")
        return []

    def odds_of(c):
        return odds_map.get(c)

    # --- 中穴・大穴(各100円) ---
    picked: set = set()

    def pick_band(lo, hi, n):
        out = []
        for combo, prob in prob_ranked:
            o = odds_of(combo)
            if combo in picked or o is None or not (lo <= o < hi):
                continue
            out.append((combo, prob, o))
            picked.add(combo)
            if len(out) >= n:
                break
        return out

    # 鉄板候補は先に予約しておき、中穴・大穴と重複させない
    teppan_candidates = [
        (c, p, odds_of(c)) for c, p in prob_ranked[:teppan_pool] if odds_of(c) is not None
    ]
    reserved = {c for c, _, _ in teppan_candidates}
    picked |= reserved
    chuuana = pick_band(*chuuana_range, n_chuuana)
    ooana = pick_band(*ooana_range, n_ooana)
    picked -= reserved

    if len(chuuana) < n_chuuana or len(ooana) < n_ooana:
        print(f"中穴{len(chuuana)}点/大穴{len(ooana)}点しか無いため見送り")
        return []

    # --- 鉄板(ガミらない金額) ---
    budget = total_stake - 100 * (n_chuuana + n_ooana)
    teppan = []  # (combo, prob, odds, stake)
    for combo, prob, o in teppan_candidates:
        if len(teppan) >= n_teppan:
            break
        need = max(100, _ceil100(total_stake / o))
        slots_left_after = n_teppan - len(teppan) - 1
        if need + 100 * slots_left_after <= budget:
            teppan.append((combo, prob, o, need))
            budget -= need

    if len(teppan) < n_teppan:
        print(f"ガミらない鉄板が{n_teppan}点そろわないため見送り(本命オッズが低すぎ)")
        return []

    if budget > 0:
        c, p, o, st = teppan[0]
        teppan[0] = (c, p, o, st + budget)

    plans: list[BetPlan] = []
    for combo, prob, o, st in teppan:
        plans.append(BetPlan("-".join(map(str, combo)), "鉄板", st, round(prob * 100, 2), o,
                             _reason_for(combo, race_df, "鉄板")))
    for label, group in (("中穴", chuuana), ("大穴", ooana)):
        for combo, prob, o in group:
            plans.append(BetPlan("-".join(map(str, combo)), label, 100, round(prob * 100, 2), o,
                                 _reason_for(combo, race_df, label)))
    return plans
