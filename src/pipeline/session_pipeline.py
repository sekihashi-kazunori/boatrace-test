from __future__ import annotations

import argparse
import os
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import lightgbm as lgb
import pandas as pd

from src.collectors.official_site import fetch_today_stadiums, fetch_race_card, fetch_odds_3rentan
from src.storage.db import get_engine, init_db, save_entries, save_bet_tickets, BetTicket
from src.features.build_features import build_feature_dataframe
from src.models.predict import predict_win_probabilities, add_original_index
from src.betting.allocation import build_bet_plan
from src.notify.notifier import notify_console, notify_discord

SESSION_RACE_NUMBERS = {
    "morning": range(1, 11),
    "day": range(1, 11),
    "nighter": range(1, 13),
}

STADIUM_NAMES = {
    "01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島",
    "05": "多摩川", "06": "浜名湖", "07": "蒲郡", "08": "常滑",
    "09": "津", "10": "三国", "11": "びわこ", "12": "住之江",
    "13": "尼崎", "14": "鳴門", "15": "丸亀", "16": "児島",
    "17": "宮島", "18": "徳山", "19": "下関", "20": "若松",
    "21": "芦屋", "22": "福岡", "23": "唐津", "24": "大村",
}


def build_message(session_name_ja: str, stadium_name: str, race_number: int, bet_plans) -> str:
    now_str = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%H:%M")
    stake_total = sum(plan.stake for plan in bet_plans)

    combo_lines = "\n".join(
        f"・{plan.combination}：{plan.stake}円"
        + (f"（{plan.odds}倍）" if plan.odds is not None else "（オッズ取得失敗）")
        for plan in bet_plans
    )

    unique_reasons = []
    for plan in bet_plans:
        if plan.reason not in unique_reasons:
            unique_reasons.append(plan.reason)
    reason_text = " / ".join(unique_reasons)

    return (
        f"【{now_str} 予想生成】\n\n"
        f"{session_name_ja}　{stadium_name}{race_number}R\n\n"
        f"{combo_lines}\n\n"
        f"投資：{stake_total}円／{len(bet_plans)}点\n\n"
        f"根拠\n{reason_text}"
    )


def run_session(session_name: str, target_date: date | None = None, model_path: str = "model.txt", db_path: str = "boatrace.db") -> None:
    target_date = target_date or date.today()

    engine = get_engine(db_path)
    init_db(engine)
    session_name_ja = {"morning": "モーニング", "day": "デイ", "nighter": "ナイター"}.get(session_name, session_name)

    stadium_codes = fetch_today_stadiums(target_date)

    if not os.path.exists(model_path):
        print(f"モデルファイルが見つかりません: {model_path} セッションを中断します。")
        return
    model = lgb.Booster(model_file=model_path)

    all_tickets: list[BetTicket] = []
    for stadium in stadium_codes:
        stadium_code = stadium["stadium_code"]

        fetched_race_numbers = []
        for race_number in SESSION_RACE_NUMBERS[session_name]:
            try:
                card = fetch_race_card(stadium_code, race_number, target_date)
            except Exception as e:
                print(f"取得失敗 {stadium_code=} {race_number=}: {e}")
                continue

            entries = to_race_entries(card, target_date, stadium_code, race_number)
            print(f"抽出結果: {len(entries)}件 中身={entries[:1]}")
            save_entries(engine, entries)
            fetched_race_numbers.append(race_number)

        if not fetched_race_numbers:
            continue

        df = build_feature_dataframe(engine, race_date=target_date)

        # exhibition_time/tilt/start_timingがDB上は文字列(object型)で
        # 保存されてしまい、LightGBMの予測時に
        # "pandas dtypes must be int, float or bool" で全滅する問題への対処。
        # 数値化できない値はNaNにしてモデル側の欠損値処理に任せる。
        for col in ("exhibition_time", "tilt", "start_timing"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        for race_number in fetched_race_numbers:
            race_df = df[
                (df.stadium_code == stadium_code) & (df.race_number == race_number)
            ]
            if race_df.empty:
                print(f"race_df空 stadium_code={stadium_code} race_number={race_number} df全体件数={len(df)}")
                continue

            try:
                odds_map = fetch_odds_3rentan(stadium_code, race_number, target_date)
            except Exception as e:
                print(f"オッズ取得失敗 {stadium_code=} {race_number=}: {e}")
                odds_map = {}

            try:
                predicted = predict_win_probabilities(model, race_df)
                indexed = add_original_index(predicted)
                bet_plans = build_bet_plan(
                    indexed,
                    total_stake=1000,
                    min_points=6,
                    max_points=8,
                    odds_map=odds_map,
                )
            except Exception as e:
                print(f"予想/買い目生成失敗 {stadium_code=} {race_number=}: {e}")
                continue

            print(f"買い目件数: {len(bet_plans)}件 indexed件数={len(indexed)}")

            stadium_name = STADIUM_NAMES.get(stadium_code, stadium_code)

            # 見送り(買い目が0件)のレースはDiscordに通知しない。コンソールログには残す。
            if not bet_plans:
                print(f"【{session_name_ja} {stadium_name} {race_number}R】見送りのため通知スキップ")
                continue

            for plan in bet_plans:
                ticket = BetTicket(
                    race_date=target_date,
                    session=session_name,
                    stadium_code=stadium_code,
                    race_number=race_number,
                    combination=plan.combination,
                    amount=plan.stake,
                )
                all_tickets.append(ticket)

            message = build_message(session_name_ja, stadium_name, race_number, bet_plans)
            try:
                notify_console(message)
                notify_discord(message)
                # Discordのレート制限(429)を避けるため、通知の間隔を空ける
                time.sleep(1.2)
            except Exception as e:
                print(f"通知失敗 {stadium_code=} {race_number=}: {e}")

    if all_tickets:
        save_bet_tickets(engine, all_tickets)


def to_race_entries(card, target_date, stadium_code, race_number):
    from src.storage.db import RaceEntry

    def g(obj, *names, default=None):
        for name in names:
            if isinstance(obj, dict):
                if name in obj:
                    return obj[name]
            elif hasattr(obj, name):
                return getattr(obj, name)
        return default

    entries = []
    odds_by_lane = {g(o, "lane_number", "boat_number", default=None): g(o, "odds", default=None) for o in card.get("odds", [])}
    before_by_lane = {
        g(b, "lane_number", "boat_number", "pit_number", default=None): b
        for b in card.get("before_info", card.get("before", []))
    }

    for entry in card.get("racers", []):
        lane = g(entry, "lane", "lane_number", "boat_number", "pit_number")
        before = before_by_lane.get(lane)
        entries.append(RaceEntry(
            race_date=target_date,
            stadium_code=stadium_code,
            race_number=race_number,
            lane_number=lane,
            racer_id=g(entry, "racer_registration_number", "racer_id"),
            national_win_rate=g(entry, "national_win_rate"),
            local_win_rate=g(entry, "local_win_rate"),
            motor_2rate=g(entry, "motor_2nd_place_rate", "motor_2rate"),
            boat_2rate=g(entry, "boat_2nd_place_rate", "boat_2rate"),
            exhibition_time=g(before, "exhibition_time") if before else None,
            tilt=g(before, "tilt") if before else None,
            start_timing=g(before, "start_timing", "average_start_timing") if before else None,
            win_odds=odds_by_lane.get(lane),
            finish_position=None,
        ))
    return entries


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", choices=["morning", "day", "nighter"], required=True)
    parser.add_argument("--action", choices=["predict", "settle"], default="predict")
    args = parser.parse_args()

    if args.action == "settle":
        print("settleアクションは未実装です。結果照合・回収率集計ロジックを別途実装する必要があります。")
    else:
        run_session(args.session, model_path="model.txt")
