from __future__ import annotations

import argparse
import os
from datetime import date

import lightgbm as lgb

from src.collectors.official_site import fetch_today_stadiums, fetch_race_card
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


def run_session(session_name: str, target_date: date | None = None, model_path: str = "model.txt", db_path: str = "boatrace.db") -> None:
    target_date = target_date or date.today()
    engine = get_engine(db_path)
    init_db(engine)
    session_name_ja = {"morning": "モーニング", "day": "デイ", "nighter": "ナイター"}.get(session_name, session_name)

    stadium_codes = fetch_today_stadiums(target_date)
    model = lgb.Booster(model_file=model_path) if os.path.exists(model_path) else None

    all_tickets: list[BetTicket] = []
    for stadium_code in stadium_codes:
        for race_number in SESSION_RACE_NUMBERS[session_name]:
            try:
                card = fetch_race_card(target_date, stadium_code, race_number)
            print(f"取得失敗 {stadium_code=} {race_number=}: {e}")
                
                continue

            entries = to_race_entries(card, target_date, stadium_code, race_number)
            save_entries(engine, entries)

            df = build_feature_dataframe(engine, race_date=target_date)
            race_df = df[
                (df.stadium_code == stadium_code) & (df.race_number == race_number)
            ]
            if race_df.empty:
                continue

            predicted = predict_win_probabilities(model, race_df)
            indexed = add_original_index(predicted)
            bet_plans = build_bet_plan(indexed, total_stake=1000, min_points=5, max_points=8)

            message_lines = [f"【{session_name_ja} {stadium_code} {race_number}R 買い目生成】"]
            for plan in bet_plans:
                ticket = BetTicket(
                    race_date=target_date,
                    session=session_name,
                    stadium_code=stadium_code,
                    race_number=race_number,
                    category=plan.category,
                    combination=plan.combination,
                    stake=plan.stake,
                    predicted_prob=plan.predicted_prob,
                    reason=plan.reason,
                )
                all_tickets.append(ticket)
                message_lines.append(
                    f"【{plan.category}】{plan.combination} {plan.stake}円 "
                    f"(予測勝率 {plan.predicted_prob}%) 理由: {plan.reason}"
                )
            message = "\n".join(message_lines)
            notify_console(message)
            notify_discord(message)

    if all_tickets:
        save_bet_tickets(engine, all_tickets)


def to_race_entries(card, target_date, stadium_code, race_number):
    from src.storage.db import RaceEntry

    def g(obj, *names, default=None):
        for name in names:
            if hasattr(obj, name):
                return getattr(obj, name)
        return default

    entries = []
    odds_by_lane = {g(o, "lane_number", "boat_number", default=None): g(o, "odds", "win_odds", default=None) for o in getattr(card.odds, "items", card.odds) if hasattr(card, "odds")} if card.odds else {}

    for entry in card.entries:
        lane = g(entry, "lane_number", "boat_number", "pit_number")
        before = next(
            (b for b in card.before_info if g(b, "lane_number", "boat_number") == lane),
            None,
        )
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
