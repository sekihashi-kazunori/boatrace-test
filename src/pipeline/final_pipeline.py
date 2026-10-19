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

from src.collectors.official_site import fetch_race_card, fetch_before_info, fetch_odds_3rentan, LAST_WEATHER, fetch_race_deadline_times, _classify_session_from_first_deadline
from src.storage.db import (
    get_engine, init_db, save_entries, save_bet_tickets, get_state_engine,
    BetTicket, RaceDecision, OddsSnapshot, RaceMeta,
)
from src.features.build_features import build_feature_dataframe
from src.models.predict import predict_win_probabilities, add_original_index
from src.betting.allocation import build_bet_plan, experimental_bets, BetPlan, build_popular_plan
from src.notify.notifier import notify_console, notify_discord
from src.pipeline.session_pipeline import build_message, plan_path, to_race_entries, STADIUM_NAMES

JST = ZoneInfo("Asia/Tokyo")
SESSIONS = {"morning": "モーニング", "day": "デイ", "nighter": "ナイター"}

# 2026-10-14: GitHubの5分おき実行が実際には20〜100分おきにしか動かず、34R取りこぼしたため窓を広げる
# 2026-10-15: 常駐見張り(3分おき)になったので、締切10分前まで待って最終予想する。
# 早い時刻のオッズは票が集まりきっておらず集中度が低く出る(10/15は全43Rで最大41.8%)ため、
# できるだけ締切直前のオッズで判断する。展示タイムは締切15〜20分前には出ている。
WAIT_IF_MORE_THAN_MIN = 10
FORCE_IF_LESS_THAN_MIN = 6
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


def _run_experiment(state, target_date, session_name, stadium_code, race_number, label, indexed, odds_map) -> None:
    """実験枠(紙上のみ)。本番の収支とは bet_type='exp' で分けて記録する。"""
    try:
        plans = experimental_bets(indexed, odds_map)
    except Exception as e:
        print(f"{label}: 実験枠の計算失敗 {e}")
        return
    if not plans:
        return
    try:  # 実験枠の失敗で本番の買い目が止まらないようにする
        save_bet_tickets(state, [
            BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                      race_number=race_number, combination=p.combination, amount=p.stake,
                      bet_type="exp", odds=p.odds)
            for p in plans
        ])
    except Exception as e:
        print(f"{label}: 実験枠の保存失敗 {e}")
        return
    # 1日30点前後になるため、Discordには個別に流さず、夜の精算で累積だけ報告する
    print(f"【実験枠】{label} " + " / ".join(f"{p.combination} {p.odds}倍({p.reason})" for p in plans))


EDOGAWA_COMBOS = [f"1-{a}-{b}" for a in (4, 5, 6) for b in (4, 5, 6) if a != b]
# 2026-10-12追加: 4-5-6ボックス。1-456-456と合わせた12点(1,200円)は江戸川1〜10月で回収率95%(1〜4Rは108%)
EDOGAWA_BOX456 = [f"{a}-{b}-{c}" for a in (4, 5, 6) for b in (4, 5, 6) for c in (4, 5, 6) if len({a, b, c}) == 3]


def _save_meta(state, target_date, stadium_code, race_number, b1_class, weather=None) -> None:
    weather = weather or {}
    try:
        with OrmSession(state) as db:
            db.add(RaceMeta(race_date=str(target_date), stadium_code=stadium_code,
                            race_number=race_number, b1_class=b1_class,
                            wind=weather.get("wind"), wave=weather.get("wave"), weather=weather.get("weather")))
            db.commit()
    except Exception as e:
        print(f"レース情報の保存失敗 {stadium_code} {race_number}R: {e}")


def _odds(odds_map, c):
    return (odds_map or {}).get(tuple(map(int, c.split("-"))))


def _rule_plans(stadium_code, race_number, b1_class, odds_map, weather=None):
    """本番の条件ルール。1〜10月約4.3万Rの検証で、前半(1〜6月)・後半(7〜10月)の両方で
    回収率100%超えだったものだけ。

    - 江戸川 1〜4R・1号艇B級・波5cm以下: 1-2-3/1-3-2 各200円 + 1-456-456 各100円(1,000円)
        過去 回収率164%(前半153%・後半182%)。波6cm以上は78%に落ちるため見送り
    - 津 全R・1号艇B級: 1-456-456 各100円(600円)。風5m以上か波3cm以上なら各200円(1,200円)
        過去 的中率5.1% 回収率127%(前半117%・後半142%)
    それ以外(1号艇A級、江戸川5R以降など)は見送り。
    """
    if not (b1_class and b1_class.startswith("B")):
        return None
    wave = (weather or {}).get("wave")
    if stadium_code == "03" and race_number <= 4 and wave is not None and wave >= 6:
        # 波6cm以上は前半97%・後半48%と悪化するため見送り(2026-10-12追加)
        print(f"江戸川{race_number}R: 波高{wave}cmで荒れすぎのため見送り")
        return None
    if stadium_code == "03" and race_number <= 4:
        return [BetPlan(c, "江戸川鉄板", 200, 0.0, _odds(odds_map, c), "1号艇が逃げて2・3号艇が続く形")
                for c in ("1-2-3", "1-3-2")] + \
               [BetPlan(c, "江戸川穴", 100, 0.0, _odds(odds_map, c), f"江戸川前半・1号艇{b1_class}は逃げにくく外が届く")
                for c in EDOGAWA_COMBOS]
    # 2026-10-17 ご主人様と決定: 津は停止。1〜10月の127%は上位5本の万舟頼み(抜くと53%)、
    # 月別も40〜60%の月が大半で、10/14以降も28%だったため。
    if False and stadium_code == "09":
        wind = (weather or {}).get("wind")
        rough = (wind is not None and wind >= 5) or (wave is not None and wave >= 3)
        # 強風(5m以上)か波3cm以上の日は金額2倍(過去268%・ただし前半90%/後半520%とムラがあるため、
        # 見送りではなく「金額で強弱」をつける。2026-10-12)
        stake = 200 if rough else 100
        note = f"・風{wind}m/波{wave}cmで外が届きやすい" if rough else ""
        return [BetPlan(c, "津穴", stake, 0.0, _odds(odds_map, c), f"津・1号艇{b1_class}は逃げにくく外が届く{note}")
                for c in EDOGAWA_COMBOS]
    return None


def _run_general(state, target_date, session_name, stadium_code, race_number, label, bet_plans) -> None:
    """本予想(2026-10-13〜、旧 参考枠): 汎用モデルの鉄板5点。bet_type='gen' のまま記録し、収支は江戸川・津と分けて報告。"""
    if not bet_plans:
        return
    try:
        save_bet_tickets(state, [
            BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                      race_number=race_number, combination=p.combination, amount=p.stake,
                      bet_type="gen", odds=p.odds)
            for p in bet_plans
        ])
    except Exception as e:
        print(f"{label}: 参考枠の保存失敗 {e}")


def _run_tsu(state, target_date, session_name, race_number, odds_map, b1_class) -> None:
    if not (b1_class and b1_class.startswith("B")):
        return
    try:
        save_bet_tickets(state, [
            BetTicket(race_date=str(target_date), session=session_name, stadium_code="09",
                      race_number=race_number, combination=c, amount=100, bet_type="tsu",
                      odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
            for c in EDOGAWA_COMBOS
        ])
    except Exception as e:
        print(f"津枠の保存失敗 {race_number}R: {e}")


def _run_edogawa(state, target_date, session_name, race_number, odds_map) -> None:
    """江戸川枠(紙上のみ・2026-10-12〜): 江戸川の全レースで 1-456-456 の6点を100円ずつ。
    1〜10月の江戸川約1,550Rで回収率97%(1〜4Rは143%)。本物かを検証する。"""
    try:
        save_bet_tickets(state, [
            BetTicket(race_date=str(target_date), session=session_name, stadium_code="03",
                      race_number=race_number, combination=c, amount=100, bet_type="edo",
                      odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
            for c in EDOGAWA_COMBOS
        ] + [
            BetTicket(race_date=str(target_date), session=session_name, stadium_code="03",
                      race_number=race_number, combination=c, amount=100, bet_type="edo456",
                      odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
            for c in EDOGAWA_BOX456
        ])
    except Exception as e:
        print(f"江戸川枠の保存失敗 {race_number}R: {e}")


# ===== 検証枠(紙上・2026-10-14〜) =====
# 2026年1〜10月の約4.4万レースを前半(1〜6月)・後半(7〜10月)に分け、
# どちらでも回収率115%以上だった「会場×1号艇の級×R帯×買い方」。全て100円/点。
# Discordへの個別配信はせず、ナイター後の最終収支報告で累積だけ出す。
def _perm(heads, seconds, thirds):
    return [f"{a}-{b}-{c}" for a in heads for b in seconds for c in thirds if len({a, b, c}) == 3]


# 本予想の絞り込み(2026-10-17 ご主人様と決定・お試し):
# 各セッション(モーニング・デイ・ナイター)最大5Rまで、集中度の基準も0.40→0.45に上げる。
# 8/1〜9/18の確定オッズ7,600Rの試算: 上限なし(集中度0.50相当)13R/日・回収率80% →
# 1セッション5Rまでにすると 約11R/日・的中率67%・回収率80〜83%。モーニング増額は見送り(過去はむしろ弱い)。
MAIN_MIN_CONC = 0.45
MAIN_MAX_PER_SESSION = 5


def _main_count(state, target_date, session_name) -> int:
    """その日・そのセッションで本予想(gen)を買ったレース数。"""
    with OrmSession(state) as db:
        rows = db.execute(select(BetTicket.stadium_code, BetTicket.race_number).where(
            BetTicket.race_date == str(target_date), BetTicket.session == session_name,
            BetTicket.bet_type == "gen").distinct()).all()
    return len(rows)


VERIFY_RULES = [
    # (bet_type, 場コード, 1号艇の級の頭文字, 最小R, 最大R, 買い目, 表示名)
    # 2026-10-20 全データ洗い直し(1〜10月 約4.4万R × 約8,400通り)で入れ替え。
    # 判定: 1〜3月/4〜6月/7〜10月の3期の回収率と、上位3本の大当たりを抜いた回収率。
    ("v_edo14", "03", "B", 1, 4, _perm([1], [4], [2, 3, 5, 6]), "江戸川 B級1〜4R 1-4-全"),          # 190% 上位3抜き82%
    ("v_edo1414", "03", "B", 1, 4, _perm([1, 4], [1, 4], [2, 3, 5, 6]), "江戸川 B級1〜4R 14-14-全"),  # 165% 3期113〜257% 上位3抜き106% ★
    ("v_suminoe", "12", "A1", 5, 8, ["1-2-3", "1-3-2"], "住之江 A1 5〜8R 1-23-23"),                   # 121% 3期116〜130% 上位3抜き100% ★
    ("v_maru", "15", "A1", 5, 8, _perm([1], [2], [3, 4, 5, 6]), "丸亀 A1 5〜8R 1-2-全"),              # 127% 上位3抜き95%
    ("v_heiwa", "04", "A1", 9, 12, ["1-2-3", "1-3-2"], "平和島 A1 9〜12R 1-23-23"),                   # 137% 上位3抜き99%
    ("v_fukuoka", "22", "A1", 5, 8, _perm([1], [2, 3, 4], [2, 3, 4]), "福岡 A1 5〜8R 1-234-234"),     # 131% 上位3抜き98%
    # 2026-10-20 ご主人様OK「宝探し枠」: 児島。的中が少なく偶然の可能性も高いので紙上で確かめる
    ("v_kojima456", "16", "B", 9, 12, _perm([1], [4, 5, 6], [4, 5, 6]), "児島 B級 9〜12R 1-456-456"),  # 197% 88R 7〜10月79%
    ("v_kojima516", "16", "", 1, 12, ["5-1-6"], "児島 全R 5-1-6"),                                   # 181% 3期144〜204% 的中16回
    # 停止(2026-10-20): 三国 3-1-全・浜名湖 23-23-全・唐津/桐生 123BOX は大当たり頼み or 期間でムラ
]
VERIFY_STADIUM_NAMES = {"15": "丸亀", "04": "平和島", "12": "住之江", "22": "福岡", "16": "児島"}


def _outer_faster(before_info) -> bool | None:
    """展示タイムで外(4〜6号艇の平均)が内(2・3号艇の平均)より速いか。6艇そろわなければ None。"""
    ex = {b.get("lane"): b.get("exhibition_time") for b in (before_info or [])}
    if any(ex.get(i) is None for i in range(2, 7)):
        return None
    return (ex[2] + ex[3]) / 2 > (ex[4] + ex[5] + ex[6]) / 3


def _run_tsu_ex(state, target_date, session_name, race_number, b1_class, odds_map, before_info) -> None:
    """検証枠(2026-10-16〜): 津・1号艇B級 かつ 展示で外が内より速いレースだけ 1-456-456。
    1〜10月: 前半185%・後半229%(津B級全体は117%・142%)。的中が前半10回・後半5回と少ないので実戦で確認する。"""
    if not (b1_class and b1_class.startswith("B")) or not _outer_faster(before_info):
        return
    try:
        save_bet_tickets(state, [
            BetTicket(race_date=str(target_date), session=session_name, stadium_code="09",
                      race_number=race_number, combination=c, amount=100, bet_type="v_tsu_ex",
                      odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
            for c in EDOGAWA_COMBOS
        ])
        print(f"検証枠 津B級+外の展示が速い: {race_number}R 記録")
    except Exception as e:
        print(f"津(展示)枠の保存失敗 {race_number}R: {e}")


def _run_verify(state, target_date, session_name, stadium_code, race_number, b1_class, odds_map) -> None:
    for bt, st, cls, rmin, rmax, combos, _title in VERIFY_RULES:
        if st != stadium_code or not (rmin <= race_number <= rmax):
            continue
        if not (b1_class and b1_class.startswith(cls)):
            continue
        try:
            save_bet_tickets(state, [
                BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                          race_number=race_number, combination=c, amount=100, bet_type=bt,
                          odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
                for c in combos
            ])
            print(f"検証枠 {_title}: {race_number}R 記録")
        except Exception as e:
            print(f"検証枠の保存失敗 {bt} {race_number}R: {e}")


def _verify_races(target_date: date) -> list:
    """2026-10-15〜: 全国の全レースを候補にする(本予想は全レースから集中度で選ぶため)。
    朝の厳選(plans)の作成が遅れると、その前のレースを丸ごと取りこぼしていた(10/15に17R)。
    plans/日付_all.json に保存。検証枠の会場は verify=True を付ける。"""
    path = os.path.join("plans", f"{target_date}_all.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = []
        failed = False
        for st, name in STADIUM_NAMES.items():
            ranges = [(r[3], r[4]) for r in VERIFY_RULES if r[1] == st]
            try:
                times = fetch_race_deadline_times(st, target_date)
            except Exception as e:
                print(f"{name}: 締切時刻取得失敗 {e}")
                times = {}
                failed = True
            for rn, t in (times or {}).items():
                data.append({"stadium_code": st, "race_number": rn, "stadium_name": name, "deadline": t,
                             "main": False, "gen": False,
                             "verify": any(lo <= rn <= hi for lo, hi in ranges)})
        if not failed and data:  # 取得失敗があれば保存せず、次回に取り直す
            os.makedirs("plans", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        print(f"全国の対象レース: {len(data)}R")
    return [(_session_by_time(r.get("deadline")), r) for r in data if r["stadium_code"] not in RULE_STADIUM_NAMES]


RULE_STADIUM_NAMES = {"03": "江戸川", "09": "津"}


def _rule_races(target_date: date) -> list:
    """江戸川・津の全レースを、締切時刻から直接 本番候補にする。一度取ったら plans/日付_rules.json に保存。"""
    path = os.path.join("plans", f"{target_date}_rules.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = []
        for st, name in RULE_STADIUM_NAMES.items():
            try:
                times = fetch_race_deadline_times(st, target_date)
            except Exception as e:
                print(f"{name}: 締切時刻取得失敗 {e}")
                times = {}
            if not times:
                continue
            sess = _classify_session_from_first_deadline(times)
            for rn, t in times.items():
                data.append({"session": sess, "stadium_code": st, "race_number": rn,
                             "stadium_name": name, "deadline": t, "main": True, "gen": False})
        os.makedirs("plans", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        print(f"勝負会場の対象レース: {len(data)}R")
    return [(_session_by_time(r.get("deadline")), r) for r in data]


def _session_by_time(deadline: str | None) -> str:
    """ご主人様の区分: 12時より前=モーニング、12〜17時=デイ、17時以降=ナイター(レースごとの締切時刻で判定)。"""
    try:
        h = int((deadline or "").split(":")[0])
    except ValueError:
        return "day"
    if h < 12:
        return "morning"
    if h < 17:
        return "day"
    return "nighter"


def run_final(target_date: date | None = None, model_path: str = "model.txt", db_path: str = "boatrace.db") -> None:
    now = datetime.now(JST)
    target_date = target_date or now.date()

    state = get_state_engine()
    with OrmSession(state) as db:
        done = {
            (d.stadium_code, d.race_number)
            for d in db.scalars(select(RaceDecision).where(RaceDecision.race_date == str(target_date)))
        }

    # 江戸川・津(条件ルール) → 検証枠 → 本予想(gen) の順に集め、同じレースはフラグを合体する
    merged: dict = {}
    for session_name, race in _rule_races(target_date) + _verify_races(target_date):
        k = (race["stadium_code"], race["race_number"])
        if k in done:
            continue
        if k in merged:
            merged[k][1]["verify"] = merged[k][1].get("verify") or race.get("verify", False)
        else:
            merged[k] = (session_name, dict(race))
    for session_name in SESSIONS:
        path = plan_path(target_date, session_name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for race in json.load(f):
                k = (race["stadium_code"], race["race_number"])
                if k in done:
                    continue
                if k in merged:
                    merged[k][1]["gen"] = merged[k][1].get("gen") or race.get("gen", False)
                elif k[0] not in RULE_STADIUM_NAMES:  # 勝負会場は _rule_races 側で扱う
                    merged[k] = (session_name, dict(race))
    todo = list(merged.values())

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
            if mins is not None and mins < 10:
                _record(state, target_date, session_name, stadium_code, race_number, "missed")
            continue

        is_main = race.get("main", True)
        if not (race.get("verify") and not race.get("gen") and not is_main):
            _run_experiment(state, target_date, session_name, stadium_code, race_number, label, indexed, odds_map)
        # ===== 本予想(2026-10-14〜): 人気上位5点に票が集中しているレースだけ =====
        # 対象は今処理している全レース(厳選候補・江戸川津・検証枠)。モデルの鉄板5点は使わない。
        pop_plans, conc = build_popular_plan(odds_map, min_concentration=MAIN_MIN_CONC)
        print(f"{label}: 集中度{conc * 100:.1f}%")
        if pop_plans and _main_count(state, target_date, session_name) >= MAIN_MAX_PER_SESSION:
            print(f"{label}: 本予想は{session_ja}で既に{MAIN_MAX_PER_SESSION}R買ったので見送り")
            pop_plans = []
        if pop_plans:
            _run_general(state, target_date, session_name, stadium_code, race_number, label, pop_plans)
            ref = build_message(session_ja, stadium_name, race_number, pop_plans, deadline_time=race.get("deadline"))
            ref = ref.replace("予想生成】", "本予想】", 1)
            try:
                notify_console(ref)
                notify_discord(ref)
                time.sleep(1.2)
            except Exception as e:
                print(f"通知失敗 {label}: {e}")
        b1_class = next((r.get("racer_class") for r in card.get("racers", []) if r.get("lane") == 1), None)
        weather = LAST_WEATHER.get((stadium_code, race_number, str(target_date)), {})
        # 全レースで1号艇の級と天候・風・波を記録(今後の条件づくりの材料)
        _save_meta(state, target_date, stadium_code, race_number, b1_class, weather)
        if stadium_code == "03":
            _run_edogawa(state, target_date, session_name, race_number, odds_map)
        _run_verify(state, target_date, session_name, stadium_code, race_number, b1_class, odds_map)

        # ===== 本番(2026-10-12 再構築): 会場×1号艇の級の条件ルール =====
        rule_plans = _rule_plans(stadium_code, race_number, b1_class, odds_map, weather) if is_main else None
        if is_main and not rule_plans:
            print(f"{label}: 条件外(1号艇{b1_class})のため見送り")
            _record(state, target_date, session_name, stadium_code, race_number, "rule_skip")
            continue
        if is_main:
            bet_plans = rule_plans
        if not is_main:
            _record(state, target_date, session_name, stadium_code, race_number, "exp_only")
            continue

        if not bet_plans:
            msg = f"【{label}】展示後の最終判断で見送り"
            notify_console(msg)
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
        # 2026-10-14 ご主人様指示: 江戸川・津は検証扱い。Discordには流さず最終収支報告で累積だけ出す
        notify_console(message)


if __name__ == "__main__":
    run_final()
