from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations

import pandas as pd

# 軸(1着候補)の全国勝率がこれを下回ったら見送り(根拠不足と判断)。
# 値は暫定です。実際のデータ分布を見ながら調整してください。
DEFAULT_MIN_AXIS_WIN_RATE = 5.5

# 直前情報が反映されているかどうかの判定に使うカラム
BEFORE_INFO_COLUMNS = ("exhibition_time", "tilt", "start_timing")


@dataclass
class BetPlan:
    combination: str
    category: str
    stake: int
    predicted_prob: float
    reason: str


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


def _reason_for(combo: tuple[int, int, int], race_df: pd.DataFrame, category: str) -> str:
    first = race_df[race_df["lane_number"] == combo[0]].iloc[0]
    parts = [f"{combo[0]}号艇を軸"]
    if (first.get("course_base_win_rate") or 0) >= 0.5:
        parts.append("イン逃げの信頼度が高い")
    if (first.get("exhibition_rank") or 99) <= 2:
        parts.append("展示タイム上位で足が良い")
    if (first.get("motor_2rate") or 0) >= 40:
        parts.append("モーター2連率が良い")
    if category == "大穴":
        parts.append("人気薄だが狙い目")
    elif category == "中穴":
        parts.append("2・3着争いの縺れを想定")
    return "、".join(parts)


def _has_exhibition_data(race_df: pd.DataFrame) -> bool:
    for col in BEFORE_INFO_COLUMNS:
        if col in race_df.columns and race_df[col].notna().any():
            return True
    return False


def build_bet_plan(
    race_df: pd.DataFrame,
    total_stake: int = 1000,
    min_points: int = 5,
    max_points: int = 8,
    min_axis_win_rate: float | None = DEFAULT_MIN_AXIS_WIN_RATE,
    fallback_stake_without_exhibition: int = 1000,
) -> list[BetPlan]:
    win_probs = dict(zip(race_df["lane_number"], race_df["predicted_win_prob"]))
    combo_probs = _harville_trifecta_probs(win_probs)
    ranked = sorted(combo_probs.items(), key=lambda x: x[1], reverse=True)

    if not ranked:
        print("有効な組み合わせが無いため見送り")
        return []

    # --- 見送り判定: 軸の信頼性が低すぎる ---
    axis_lane = ranked[0][0][0]
    axis_row = race_df[race_df["lane_number"] == axis_lane]
    axis_win_rate = axis_row.iloc[0].get("national_win_rate") if not axis_row.empty else None

    if min_axis_win_rate is not None and axis_win_rate is not None and axis_win_rate < min_axis_win_rate:
        print(f"軸{axis_lane}号艇の全国勝率{axis_win_rate}が閾値{min_axis_win_rate}未満のため見送り")
        return []

    # --- 投資額抑制: 直前情報が未反映 ---
    has_exhibition = _has_exhibition_data(race_df)
    if not has_exhibition and total_stake > fallback_stake_without_exhibition:
        print(f"直前情報未反映のため投資額を{total_stake}円→{fallback_stake_without_exhibition}円に抑制")
        total_stake = fallback_stake_without_exhibition

    tetsuban = ranked[:2]
    chuuketsu = ranked[2:5]
    anaba_candidates = [item for item in ranked if item[0][0] in (3, 4, 5, 6)]
    ooana = anaba_candidates[:2] if anaba_candidates else ranked[5:7]

    # 鉄板→中穴→大穴の優先順で、重複する買い目は先に採用された方だけ残す
    seen: set[tuple[int, int, int]] = set()
    categorized: list[tuple[tuple[int, int, int], float, str]] = []

    for combo, prob in tetsuban:
        if combo not in seen:
            categorized.append((combo, prob, "鉄板"))
            seen.add(combo)

    chuuketsu_slots = max(1, min(3, max_points - 4))
    for combo, prob in chuuketsu[:chuuketsu_slots]:
        if combo not in seen:
            categorized.append((combo, prob, "中穴"))
            seen.add(combo)

    for combo, prob in ooana:
        if combo not in seen:
            categorized.append((combo, prob, "大穴"))
            seen.add(combo)

    categorized = categorized[:max_points]

    # 最低点数に満たない分は、確率順に「中穴」として補充する
    # (旧ロジックは補充分を無条件で「大穴」扱いしており、実際の確率と表示カテゴリがズレていた)
    if len(categorized) < min_points:
        for combo, prob in ranked:
            if len(categorized) >= min_points:
                break
            if combo not in seen:
                categorized.append((combo, prob, "中穴"))
                seen.add(combo)

    n_tetsuban = sum(1 for _, _, c in categorized if c == "鉄板")
    n_chuuketsu = sum(1 for _, _, c in categorized if c == "中穴")
    n_ooana = sum(1 for _, _, c in categorized if c == "大穴")

    stake_tetsuban_total = int(total_stake * 0.5)
    stake_chuuketsu_total = int(total_stake * 0.3)
    stake_ooana_total = total_stake - stake_tetsuban_total - stake_chuuketsu_total

    stake_totals = {"鉄板": stake_tetsuban_total, "中穴": stake_chuuketsu_total, "大穴": stake_ooana_total}
    counts = {"鉄板": n_tetsuban, "中穴": n_chuuketsu, "大穴": n_ooana}

    plans: list[BetPlan] = []
    for combo, prob, category in categorized:
        stake = round(stake_totals[category] / max(counts[category], 1) / 100) * 100
        plans.append(
            BetPlan(
                combination="-".join(map(str, combo)),
                category=category,
                stake=max(stake, 100),
                predicted_prob=round(prob * 100, 2),
                reason=_reason_for(combo, race_df, category),
            )
        )
    return plans
