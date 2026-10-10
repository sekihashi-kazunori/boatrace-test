"""
展示タイム後の最終予想(2026-10-10〜)。

GitHub Actions から5分おきに呼ばれ、各セッション開始時に厳選済みの
レース(plans/日付_セッション.json)のうち、締切が近いものだけを
展示タイム・直前オッズ込みで予想し直して買い目を確定・通知する。

判定ルール(締切までの残り分数):
  - 25分より先         → まだ待つ
  - 2〜25分 かつ 展示タイム6艇分そろった → 最終予想
  - 2〜15分 で展示タイムがまだ無い      → 待ちきれないので手持ちの情報で最終予想
    (GitHubの定時実行は実際には10〜17分間隔になるため、余裕を持たせている)
  - 2分未満(締切済み含む)              → 間に合わず「missed」として記録
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import lightgbm as lgb
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from src.collectors.official_site import fetch_race_card, fetch_before_info, fetch_odds_3rentan
from src.storage.db import (
    get_engine, init_db, save_entries, save_bet_tickets, get_state_engine,
    BetTicket, RaceDecision, OddsSnapshot,
)
from src.features.build_features import build_feature_dataframe
from src.models.predict import predict_win_probabilities, add_original_index
from src.betting.allocation import build_bet_plan
from src.notify.notifier import notify_console, notify_discord
from src.pipeline.session_pipeline import build_message, plan_path, to_race_entries, STADIUM_NAMES

JST = ZoneInfo("Asia/Tokyo")
SESSIONS = {"morning": "モーニング", "day": "デイ", "nighter": "ナイター"}

WAIT_IF_MORE_THAN_MIN = 25
FORCE_IF_LESS_THAN_MIN = 15
TOO_LATE_MIN = 2


def _minutes_to_deadline(now: datetime, deadline: str | None) -> float | None:
    if not deadline:
        return None
    h, m = map(int, deadline.split(":"))
    dl = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return (dl - now).total_seconds() / 60


def _record(state, target_date, session, stadium_code, race_number, status):
    with OrmSession(state) as db:
        db.add(RaceDecision(
            race_date=str(target_date), session=session,
            stadium_code=stadium_code, race_number=race_number, status=status,
        ))
        db.commit()


def _save_snapshot(state, target_date, stadium_code, race_number, odds_map, indexed) -> None:
    try:
        probs = None
        if {"p1", "p2", "p3"} <= set(indexed.columns):
            from src.betting.allocation import _harville3
            probs = {"-".join(map(str, k)): round(v, 5) for k, v in _harville3(indexed).items()}
        with OrmSession(state) as db:
            db.add(OddsSnapshot(
                race_date=str(target_date), stadium_code=stadium_code, race_number=race_number,
                odds_json=json.dumps({"-".join(map(str, k)): v for k, v in (odds_map or {}).items()}),
                probs_json=json.dumps(probs) if probs else None,
            ))
            db.commit()
    except Exception as e:
        print(f"オッズ保存失敗: {e}")


def run_final(target_date: date | None = None, model_path: str = "model.txt", db_path: str = "boatrace.db") -> None:
    now = datetime.now(JST)
    target_date = target_date or now.date()

    state = get_state_engine()
    with OrmSession(state) as db:
        done = {
            (d.stadium_code, d.race_number)
            for d in db.scalars(select(RaceDecision).where(RaceDecision.race_date == str(target_date)))
        }

    todo = []
    for session_name in SESSIONS:
        path = plan_path(target_date, session_name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for race in json.load(f):
                if (race["stadium_code"], race["race_number"]) not in done:
                    todo.append((session_name, race))

    if not todo:
        print("処理待ちの厳選レースなし")
        return

    engine = get_engine(db_path)
    init_db(engine)
    model = None

    for session_name, race in sorted(todo, key=lambda x: x[1].get("deadline") or "99:99"):
        stadium_code = race["stadium_code"]
        race_number = race["race_number"]
        stadium_name = race.get("stadium_name") or STADIUM_NAMES.get(stadium_code, stadium_code)
        session_ja = SESSIONS[session_name]
        label = f"{session_ja} {stadium_name}{race_number}R"

        mins = _minutes_to_deadline(datetime.now(JST), race.get("deadline"))
        if mins is None:
            print(f"{label}: 締切時刻不明のため今すぐ最終予想")
        elif mins > WAIT_IF_MORE_THAN_MIN:
            print(f"{label}: 締切まで{mins:.0f}分 → 待機")
            continue
        elif mins < TOO_LATE_MIN:
            print(f"{label}: 締切まで{mins:.0f}分 → 間に合わず")
            _record(state, target_date, session_name, stadium_code, race_number, "missed")
            continue

        try:
            before_info = fetch_before_info(stadium_code, race_number, target_date)
        except Exception as e:
            print(f"{label}: 直前情報取得失敗 {e}")
            before_info = None
        n_exh = sum(1 for b in (before_info or []) if b.get("exhibition_time") is not None)

        if n_exh < 6 and mins is not None and mins > FORCE_IF_LESS_THAN_MIN:
            print(f"{label}: 展示タイム{n_exh}/6艇・締切まで{mins:.0f}分 → 展示待ち")
            continue

        try:
            card = fetch_race_card(stadium_code, race_number, target_date)
            entries = to_race_entries(card, target_date, stadium_code, race_number, before_info=before_info)
            save_entries(engine, entries)

            df = build_feature_dataframe(engine, race_date=target_date)
            for col in ("exhibition_time", "tilt", "start_timing"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            race_df = df[(df.stadium_code == stadium_code) & (df.race_number == race_number)]
            if race_df.empty:
                raise ValueError("出走データが空")

            odds_map = fetch_odds_3rentan(stadium_code, race_number, target_date)

            if model is None:
                model = lgb.Booster(model_file=model_path)
            indexed = add_original_index(predict_win_probabilities(model, race_df))
            bet_plans = build_bet_plan(indexed, total_stake=1000, odds_map=odds_map)
            _save_snapshot(state, target_date, stadium_code, race_number, odds_map, indexed)
        except Exception as e:
            print(f"{label}: 最終予想失敗 {e}")
            # 締切が迫っていればあきらめる。まだ時間があれば次回再挑戦。
            if mins is not None and mins < FORCE_IF_LESS_THAN_MIN:
                _record(state, target_date, session_name, stadium_code, race_number, "missed")
            continue

        if not bet_plans:
            msg = f"【{label}】展示後の最終判断で見送り"
            notify_console(msg)
            try:
                notify_discord(msg)
            except Exception as e:
                print(f"通知失敗: {e}")
            _record(state, target_date, session_name, stadium_code, race_number, "skipped")
            continue

        tickets = [
            BetTicket(
                race_date=str(target_date), session=session_name,
                stadium_code=stadium_code, race_number=race_number,
                combination=p.combination, amount=p.stake,
            )
            for p in bet_plans
        ]
        save_bet_tickets(state, tickets)
        _record(state, target_date, session_name, stadium_code, race_number, "bought")

        exh_note = "" if n_exh >= 6 else "（展示タイム未発表のまま予想）"
        message = build_message(session_ja, stadium_name, race_number, bet_plans, deadline_time=race.get("deadline"))
        message = message.replace("予想生成】", f"展示後 最終予想】{exh_note}", 1)
        try:
            notify_console(message)
            notify_discord(message)
            time.sleep(1.2)
        except Exception as e:
            print(f"通知失敗 {label}: {e}")


if __name__ == "__main__":
    run_final()
