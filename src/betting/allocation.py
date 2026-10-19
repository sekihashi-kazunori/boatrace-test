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


def _harville3(race_df: pd.DataFrame) -> dict[tuple[int, int, int], float]:
    """1着・2着・3着の各モデルの確率から3連単の確率を出す(v2)。"""
    p1 = dict(zip(race_df["lane_number"], race_df["p1"]))
    p2 = dict(zip(race_df["lane_number"], race_df["p2"]))
    p3 = dict(zip(race_df["lane_number"], race_df["p3"]))
    out = {}
    for i, j, k in permutations(p1.keys(), 3):
        b = p2[j] / max(1e-9, 1 - p2[i])
        c = p3[k] / max(1e-9, 1 - p3[i] - p3[j])
        out[(i, j, k)] = p1[i] * b * c
    s = sum(out.values()) or 1.0
    return {key: v / s for key, v in out.items()}


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
    n_teppan: int = 5,
    max_odds: float = 30.0,
    **_ignored,
) -> list[BetPlan]:
    """
    レースの買い目と金額配分を決める(2026-10-11夜 ご主人様指定: 大穴なし・30倍以下の鉄板で120%を目指す)。

    鉄板5点 = オッズ30倍以下の中から、予測確率の高い順に5点。計1000円。
      金額はオッズの逆数に比例(低いオッズほど厚く)、100円単位・最低100円。
      → どの鉄板が当たっても払戻がだいたい同じになる。
    30倍以下が5点に満たないレースは、ある分だけで1000円を配分する。
    オッズが取得できないレースは見送り。

    8〜9月の締切時オッズ約7,600Rのバックテスト: 1日21R厳選で回収率 8月79%・9月82%
    (旧ルール 鉄板5+穴1 は77〜78%)。
    """
    if {"p1", "p2", "p3"} <= set(race_df.columns):
        combo_probs = _harville3(race_df)
    else:
        combo_probs = _harville_trifecta_probs(dict(zip(race_df["lane_number"], race_df["predicted_win_prob"])))
    if not odds_map:
        print("オッズ未取得のため見送り")
        return []

    ranked = sorted(
        ((c, p, odds_map[c]) for c, p in combo_probs.items() if odds_map.get(c) and odds_map[c] <= max_odds),
        key=lambda x: x[1], reverse=True,
    )[:n_teppan]
    if not ranked:
        print(f"{max_odds}倍以下の買い目が無いため見送り")
        return []

    inv = [1.0 / o for _, _, o in ranked]
    stakes = [max(100, int(total_stake * w / sum(inv) / 100 + 0.5) * 100) for w in inv]
    order_low = sorted(range(len(stakes)), key=lambda i: ranked[i][2])
    while sum(stakes) > total_stake:
        for i in reversed(order_low):
            if stakes[i] > 100:
                stakes[i] -= 100
                break
        else:
            break
    while sum(stakes) < total_stake:
        stakes[order_low[0]] += 100

    return [
        BetPlan("-".join(map(str, c)), "鉄板", st, round(p * 100, 2), o, _reason_for(c, race_df, "鉄板"))
        for (c, p, o), st in zip(ranked, stakes)
    ]


def experimental_bets(
    race_df: pd.DataFrame,
    odds_map: dict[tuple[int, int, int], float] | None,
    min_ratio: float = 1.7,
    max_odds: float = 30.0,
) -> list[BetPlan]:
    """
    【実験枠】(2026-10-11〜 紙上のみの検証)
    モデルの確率が、オッズから逆算した世間の確率の min_ratio 倍以上で、
    オッズが max_odds 倍以下の買い目に100円ずつ。

    8〜9月の締切時オッズ約7,600レースのバックテストでは回収率108%
    (8月102%・9月117%)だったが、90%区間は85〜131%で、近い条件
    (1.5倍・2.0倍)は100%未満だったため、偶然の可能性が高い。
    本番と完全に分けて記録し、実データで本物かどうかを確かめる。
    """
    if not odds_map or len(odds_map) < 100:
        return []
    if {"p1", "p2", "p3"} <= set(race_df.columns):
        probs = _harville3(race_df)
    else:
        probs = _harville_trifecta_probs(dict(zip(race_df["lane_number"], race_df["predicted_win_prob"])))
    inv_sum = sum(1.0 / o for o in odds_map.values() if o)
    out = []
    for combo, o in odds_map.items():
        if not o or o > max_odds or combo not in probs:
            continue
        q = (1.0 / o) / inv_sum
        ratio = probs[combo] / q
        if ratio >= min_ratio:
            out.append(BetPlan("-".join(map(str, combo)), "実験", 100, round(probs[combo] * 100, 2), o,
                               f"モデル/世間={ratio:.1f}倍"))
    return sorted(out, key=lambda b: b.odds)


def build_popular_plan(
    odds_map: dict[tuple[int, int, int], float] | None,
    total_stake: int = 1000,
    n: int = 5,
    min_concentration: float = 0.50,
) -> tuple[list[BetPlan], float]:
    """本予想(2026-10-14〜): 人気上位5点に票が集中しているレースだけ、その5点を買う。

    集中度 = 人気上位5点のオッズから出した確率(1/オッズを全120点で正規化)の合計。
    8/1〜9/18の確定オッズ約7,700Rで、集中度0.50以上は 1日約13R・的中率60%・回収率82%
    (前半80%/後半85%)。モデル予想の鉄板5点(的中率33〜50%・回収率70%台)より的中率を優先。
    金額はオッズの逆数に比例(どれが当たっても払戻がほぼ同じ)、100円単位・最低100円。
    戻り値: (買い目, 集中度)。条件外なら ([], 集中度)。
    """
    if not odds_map or len(odds_map) < 100:
        return [], 0.0
    valid = {c: o for c, o in odds_map.items() if o and o > 0}
    inv_all = sum(1.0 / o for o in valid.values())
    # 集中度はこれまで通り人気上位5点で測る(レースの選び方は変えない)
    conc = sum(1.0 / o for _, o in sorted(valid.items(), key=lambda x: x[1])[:5]) / inv_all
    if conc < min_concentration:
        return [], conc
    # 2026-10-20 ご主人様決定「ロマン枠」: 本命は上位4点・900円 + 大穴(50〜100倍の人気1位)1点・100円 = 1,000円。
    # 8/1〜9/18の1,372Rで 的中率50%・ガミ1%・回収率81%(現行 本命5+中穴2+大穴2 は76%・ガミ21%)。
    n, total_stake = 4, 900
    ranked = sorted(valid.items(), key=lambda x: x[1])[:n]

    inv = [1.0 / o for _, o in ranked]
    stakes = [max(100, int(total_stake * w / sum(inv) / 100 + 0.5) * 100) for w in inv]
    while sum(stakes) > total_stake:
        for i in reversed(range(len(stakes))):
            if stakes[i] > 100:
                stakes[i] -= 100
                break
        else:
            break
    while sum(stakes) < total_stake:
        stakes[0] += 100

    reason = f"人気上位{n}点に票が集中（集中度{conc * 100:.0f}%）"
    plans = [
        BetPlan("-".join(map(str, c)), "本命", st, round(1.0 / o / inv_all * 100, 2), o, reason)
        for (c, o), st in zip(ranked, stakes)
    ]
    # 2026-10-16 ご主人様案: 大穴2点を100円ずつ追加(計1,200円)。
    # 50〜100倍の中で人気上位2点。8/1〜9/18の確定オッズ2,612Rで、
    # 回収率は本命5点だけと同じ78.5%のまま、的中率49%→52%(50倍超の的中は約36Rに1回)。
    # 100倍超にすると回収率が75〜76%に下がるため50〜100倍に限定。
    # 2026-10-16 ご主人様指示で中穴2点(30〜50倍の人気上位2点・各100円)も追加 → 計1,400円。
    # 同じ2,612Rで 本命5+中穴2+大穴2 = 的中率56.4%・回収率77.9%(前半77.8/後半78.0)。
    for lo, hi, cat, k in ((50, 100, "大穴", 1),):
        picks = sorted(((c, o) for c, o in valid.items() if lo < o <= hi), key=lambda x: x[1])[:k]
        plans += [
            BetPlan("-".join(map(str, c)), cat, 100, round(1.0 / o / inv_all * 100, 2), o, f"{lo}〜{hi}倍の人気1位(ロマン枠)")
            for c, o in picks
        ]
    return plans, conc
