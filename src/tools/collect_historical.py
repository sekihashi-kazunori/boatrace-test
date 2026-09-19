"""
過去データ一括収集スクリプト。

boatraceopenapi/api (2026-01-01以降対応)を使って、指定期間の
全国24場・全レース分の出走表・直前情報・結果をまとめてDBに保存する。
1日につきAPIへのリクエストは1回で済む。

使い方:
    python -m src.tools.collect_historical --start 20260101 --end 20260918

引数を省略した場合は、2026-01-01から昨日までを対象にする。
"""
import argparse
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from src.collectors.official_site import fetch_day
from src.storage.db import get_engine, init_db, save_entries, save_race_result, RaceEntry

API_START_DATE = date(2026, 1, 1)


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def collect_day(engine, target_date: date) -> tuple[int, int]:
    """1日分のデータを取得してDBに保存する。(保存したレース数, うち結果確定件数) を返す"""
    try:
        data = fetch_day(target_date)
    except Exception as e:
        print(f"{target_date}: 取得失敗またはデータ無し ({e})")
        return 0, 0

    stadiums = data.get("programs", {}).get("stadiums", {})
    race_count = 0
    result_count = 0

    for stadium_key, stadium_data in stadiums.items():
        stadium_code = f"{int(stadium_key):02d}"
        races = stadium_data.get("races", {})

        for race_key, race in races.items():
            race_number = int(race_key)

            # 直前情報をレーン(進入コース)別に引けるようにしておく
            before_by_lane = {}
            preview = race.get("preview")
            if preview:
                for _, r in preview.get("racers", {}).items():
                    before_by_lane[r.get("course_number")] = r

            # 結果(着順)をレーン別に引けるようにしておく
            place_by_lane = {}
            result = race.get("result")
            if result:
                for _, r in result.get("racers", {}).items():
                    lane = r.get("course_number")
                    place = r.get("place_number")
                    if lane and place:
                        place_by_lane[lane] = place

            entries = []
            for _, r in race.get("racers", {}).items():
                lane = r.get("entry_number")
                before = before_by_lane.get(lane)
                entries.append(RaceEntry(
                    race_date=target_date,
                    stadium_code=stadium_code,
                    race_number=race_number,
                    lane_number=lane,
                    racer_id=str(r.get("number")) if r.get("number") is not None else None,
                    national_win_rate=r.get("national_win_rate"),
                    local_win_rate=r.get("local_win_rate"),
                    motor_2rate=r.get("motor_top_2_percent"),
                    boat_2rate=r.get("boat_top_2_percent"),
                    exhibition_time=before.get("exhibition_time") if before else None,
                    tilt=before.get("tilt_adjustment") if before else None,
                    start_timing=before.get("start_timing") if before else None,
                    win_odds=None,
                    finish_position=place_by_lane.get(lane),
                ))

            save_entries(engine, entries)
            race_count += 1

            if result:
                trifecta_list = result.get("payouts", {}).get("trifecta", [])
                if trifecta_list:
                    res_obj = SimpleNamespace(
                        race_date=target_date,
                        stadium_code=stadium_code,
                        race_number=race_number,
                        finish_order=trifecta_list[0].get("combination"),
                        trifecta_payout=trifecta_list[0].get("amount"),
                    )
                    save_race_result(engine, res_obj)
                    result_count += 1

    return race_count, result_count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=str, default=None, help="YYYYMMDD形式。省略時は2026-01-01")
    parser.add_argument("--end", type=str, default=None, help="YYYYMMDD形式。省略時は昨日")
    parser.add_argument("--db-path", type=str, default="boatrace.db")
    args = parser.parse_args()

    start = parse_date(args.start) if args.start else API_START_DATE
    end = parse_date(args.end) if args.end else date.today() - timedelta(days=1)

    engine = get_engine(args.db_path)
    init_db(engine)

    current = start
    total_races = 0
    total_results = 0
    total_days = 0

    while current <= end:
        race_count, result_count = collect_day(engine, current)
        total_races += race_count
        total_results += result_count
        if race_count > 0:
            total_days += 1
            print(f"{current}: {race_count}レース保存 (うち結果確定 {result_count}件)")
        current += timedelta(days=1)
        time.sleep(0.3)  # 相手サーバー(GitHub Pages)への配慮

    print(f"=== 完了: {total_days}日分、{total_races}レース、うち結果確定{total_results}件を保存しました ===")


if __name__ == "__main__":
    main()
