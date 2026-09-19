"""
ボートレース公式サイトのデータを取得するモジュール。

boatrace.jp を直接スクレイピングするのではなく、非公式のオープンAPI
(boatraceopenapi/api, GitHub Pages上でJSON公開)を利用する。
これにより、HTML構造への依存や1レースごとの複数リクエストが不要になる。

出典: https://github.com/boatraceopenapi/api (非公式・無料、BOATRACE公式とは無関係)
対応期間: 2026年01月01日以降。存在しない日付は404になる。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import requests

BASE_URL = "https://boatraceopenapi.github.io/api/v1"


def fetch_day(target_date: Optional[date] = None) -> dict:
    """
    指定日の全国24場・全レース分のデータ(出走表・直前情報・結果)を
    1回のリクエストでまとめて取得する。

    Returns:
        生のJSON(dict)。存在しない日付(対応期間外・未来日付)は
        HTTP 404 になり、例外が発生する。
    """
    if target_date is None:
        target_date = date.today()
    url = f"{BASE_URL}/{target_date.year}/{target_date.strftime('%Y%m%d')}.json"
    res = requests.get(url, timeout=15)
    res.raise_for_status()
    return res.json()


def fetch_today_stadiums(target_date: Optional[date] = None) -> list[dict]:
    """
    指定日(省略時は今日)に開催中のボートレース場一覧を取得する。

    Returns:
        [{"stadium_code": "01"}, ...]
    """
    data = fetch_day(target_date)
    stadiums = data.get("programs", {}).get("stadiums", {})
    return [{"stadium_code": f"{int(code):02d}"} for code in stadiums.keys()]


def fetch_race_card(stadium_code: str, race_number: int, target_date: Optional[date] = None) -> dict:
    """
    指定場・指定レースの出走表を取得する。

    Returns:
        {
            "stadium_code": "01",
            "race_number": 1,
            "date": "20260910",
            "racers": [
                {"lane": 1, "name": "...", "number": "...", "rank": "B1",
                 "national_win_rate": 4.25, "local_win_rate": 4.9,
                 "motor_2rate": 42.86, "boat_2rate": 37.62},
                ...
            ]
        }
    """
    if target_date is None:
        target_date = date.today()
    data = fetch_day(target_date)
    stadium_key = str(int(stadium_code))
    race = data["programs"]["stadiums"][stadium_key]["races"][str(race_number)]

    racers = []
    for _, r in race.get("racers", {}).items():
        racers.append({
            "lane": r.get("entry_number"),
            "name": r.get("name"),
            "number": r.get("number"),
            "racer_id": r.get("number"),
            "rank": r.get("rank_number_source"),  # A1 / A2 / B1 / B2
            "national_win_rate": r.get("national_win_rate"),
            "local_win_rate": r.get("local_win_rate"),
            "motor_2rate": r.get("motor_top_2_percent"),
            "boat_2rate": r.get("boat_top_2_percent"),
        })

    # 直前情報(展示タイム・チルト・ST)も同じレスポンスに含まれているので、
    # ここでまとめて埋め込む(to_race_entries側は card["before_info"] を見に行く仕様のため、
    # session_pipeline.py 側を変更しなくて済むようにしている)
    before_info = []
    preview = race.get("preview")
    if preview:
        for _, r in preview.get("racers", {}).items():
            before_info.append({
                "lane_number": r.get("course_number"),
                "exhibition_time": r.get("exhibition_time"),
                "tilt": r.get("tilt_adjustment"),
                "start_timing": r.get("start_timing"),
            })

    return {
        "stadium_code": stadium_code,
        "race_number": race_number,
        "date": target_date.strftime("%Y%m%d"),
        "racers": racers,
        "before_info": before_info,
    }


def fetch_before_info(stadium_code: str, race_number: int, target_date: Optional[date] = None) -> list[dict]:
    """
    指定場・指定レースの直前情報(展示タイム・チルト・ST)を取得する。
    レース開始前でまだ直前情報が無い場合は空リストを返す。

    Returns:
        [{"lane_number": 1, "exhibition_time": 6.81, "tilt": -0.5, "start_timing": 0.18}, ...]
    """
    if target_date is None:
        target_date = date.today()
    data = fetch_day(target_date)
    stadium_key = str(int(stadium_code))
    race = data["programs"]["stadiums"][stadium_key]["races"][str(race_number)]
    preview = race.get("preview")
    if not preview:
        return []

    before_entries = []
    for _, r in preview.get("racers", {}).items():
        before_entries.append({
            "lane_number": r.get("course_number"),
            "exhibition_time": r.get("exhibition_time"),
            "tilt": r.get("tilt_adjustment"),
            "start_timing": r.get("start_timing"),
        })
    return before_entries


def fetch_race_result(stadium_code: str, race_number: int, target_date: Optional[date] = None) -> Optional[dict]:
    """
    指定場・指定レースの結果(着順・3連単払戻金)を取得する。
    まだ結果未確定の場合は None を返す。

    Returns:
        {
            "stadium_code": "01",
            "race_number": 1,
            "date": "20260910",
            "finish_order": [4, 3, 1, 5, 2, 6],  # 1着から順に艇番(進入コース)
            "trifecta_combination": "4-3-1",
            "trifecta_payout": 3040,
            "trifecta_popularity": None,
        }
    """
    if target_date is None:
        target_date = date.today()
    data = fetch_day(target_date)
    stadium_key = str(int(stadium_code))
    race = data["programs"]["stadiums"][stadium_key]["races"][str(race_number)]
    result = race.get("result")
    if not result:
        return None

    finish_order = [None] * 6
    for _, r in result.get("racers", {}).items():
        place = r.get("place_number")
        lane = r.get("course_number")
        if place and lane and 1 <= place <= 6:
            finish_order[place - 1] = lane
    finish_order = [x for x in finish_order if x is not None]

    trifecta_list = result.get("payouts", {}).get("trifecta", [])
    trifecta_combination = trifecta_list[0].get("combination") if trifecta_list else None
    trifecta_payout = trifecta_list[0].get("amount") if trifecta_list else None

    return {
        "stadium_code": stadium_code,
        "race_number": race_number,
        "date": target_date.strftime("%Y%m%d"),
        "finish_order": finish_order,
        "trifecta_combination": trifecta_combination,
        "trifecta_payout": trifecta_payout,
        "trifecta_popularity": None,
    }
