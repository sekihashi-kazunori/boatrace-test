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


def build_bet_plan(
    race_df: pd.DataFrame,
    total_stake: int = 1000,
    min_points: int = 6,
    max_points: int = 8,
    odds_map: dict[tuple[int, int, int], float] | None = None,
    max_stake_fraction: float = 0.4,
) -> list[BetPlan]:
    """
    レースの買い目と金額配分を決める。

    設計方針(2026-10-09改訂):
    以前は「予測確率の高さ」だけで上位の買い目を選び、本命(鉄板)に
    機械的に総額の50%を割り当てていた。これだと、当たっても配当の
    小さい本命ばかりに資金が寄ってしまい、回収率が伸びなかった
    (例: 2026-10-09 デイ 10R中3R的中、回収率33.7%)。

    オッズ取得が直ったので、「予測確率 × オッズ」= 期待値(1点あたり
    モデルが見積もる期待配当)が高い買い目を優先して選び、期待値に
    比例して資金を配分する方式に変更。1点への資金集中は
    max_stake_fraction で上限を設けて抑える。

    オッズが取得できなかった場合(取得失敗時)は、従来通り確率ベースの
    選定・配分にフォールバックする。
    """
    print(f"race_df lane_number列: {race_df['lane_number'].tolist()}")
    win_probs = dict(zip(race_df["lane_number"], race_df["predicted_win_prob"]))
    print(f"win_probs件数: {len(win_probs)} 中身={win_probs}")
    combo_probs = _harville_trifecta_probs(win_probs)

    # 根拠文言用のカテゴリは、確率順位で決める(採用順=期待値順とは別)。
    prob_ranked = sorted(combo_probs.items(), key=lambda x: x[1], reverse=True)
    prob_rank_of = {combo: rank for rank, (combo, _) in enumerate(prob_ranked)}

    has_odds = bool(odds_map)

    if has_odds:
        ev_candidates = [
            (combo, prob, odds_map[combo])
            for combo, prob in combo_probs.items()
            if odds_map.get(combo) is not None
        ]
        ev_candidates.sort(key=lambda x: x[1] * x[2], reverse=True)
        print(f"期待値ベース選定: オッズ取得済み{len(ev_candidates)}点/全{len(combo_probs)}点中から選定")

        # オッズが一部の点でしか取れていない場合、min_pointsに届かない
        # ことがあるため、確率上位の点(オッズ無し)で不足分を補う。
        if len(ev_candidates) < min_points:
            existing = {combo for combo, _, _ in ev_candidates}
            for combo, prob in prob_ranked:
                if combo in existing:
                    continue
                ev_candidates.append((combo, prob, None))
                existing.add(combo)
                if len(ev_candidates) >= min_points:
                    break
    else:
        ev_candidates = [(combo, prob, None) for combo, prob in prob_ranked]
        print("オッズ未取得のため確率ベースの選定にフォールバック")

    if not ev_candidates:
        return []

    n_points = max(min_points, min(max_points, len(ev_candidates)))
    selected = ev_candidates[:n_points]

    # 期待値 = 確率 × オッズ。オッズ不明の点は保守的にオッズ1.0とみなす
    # (勝率相応にしか配当が無いと仮定し、資金を過剰に寄せないため)。
    weights = [prob * (odds if odds is not None else 1.0) for _, prob, odds in selected]

    weight_sum = sum(weights)
    if weight_sum <= 0:
        weights = [1.0] * len(selected)
        weight_sum = float(len(selected))

    # 1点への資金集中を避けるため、1点あたりの配分比率に上限を設ける。
    max_single = total_stake * max_stake_fraction
    raw_stakes = [total_stake * w / weight_sum for w in weights]
    raw_stakes = [min(s, max_single) for s in raw_stakes]

    # 上限でカットした分を、上限に達していない点に比率配分し直す。
    shortfall_from_cap = total_stake - sum(raw_stakes)
    if shortfall_from_cap > 0:
        uncapped_idx = [i for i, s in enumerate(raw_stakes) if s < max_single]
        uncapped_weight_sum = sum(weights[i] for i in uncapped_idx) or 1.0
        for i in uncapped_idx:
            raw_stakes[i] += shortfall_from_cap * weights[i] / uncapped_weight_sum

    # 100円単位に丸め、最低100円を保証
    stakes = [max(100, round(s / 100) * 100) for s in raw_stakes]

    # 丸め誤差をtotal_stakeに合わせる(最大配分点に寄せる)
    diff = total_stake - sum(stakes)
    if diff != 0 and stakes:
        max_idx = stakes.index(max(stakes))
        stakes[max_idx] = max(100, stakes[max_idx] + diff)

    plans: list[BetPlan] = []
    for (combo, prob, odds), stake in zip(selected, stakes):
        category = _category_for_rank(prob_rank_of.get(combo, 99))
        plans.append(
            BetPlan(
                combination="-".join(map(str, combo)),
                category=category,
                stake=stake,
                predicted_prob=round(prob * 100, 2),
                odds=odds,
                reason=_reason_for(combo, race_df, category),
            )
        )

    return plans
