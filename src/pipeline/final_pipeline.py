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
# 2026-10-30 ご主人様指示「必ずモーニング・デイ・ナイターで10レースは予想を出す」
MAIN_MAX_PER_SESSION = 10
MAIN_TARGET_PER_SESSION = 10
# 締切前オッズの集中度(上位5点)の分布(10/11〜10/29の2,419R)。CONC_Q[i] より上のレースが全体の (i+1)*5%
CONC_Q = [0.466, 0.429, 0.403, 0.383, 0.366, 0.35, 0.337, 0.325, 0.311, 0.297, 0.284, 0.273, 0.261, 0.248,
          0.235, 0.222, 0.21, 0.193, 0.172]


def _quota_threshold(need: int, remaining: int) -> float:
    """残りレースの中から need レース選ぶための集中度の下限。
    集中度0.45以上はいつでも買う。足りなさそうなら、残りの上位 need/remaining の割合まで下げる。"""
    if need <= 0:
        return 9.0
    if remaining <= need:
        return 0.0
    frac = need / remaining
    i = min(len(CONC_Q) - 1, max(0, int(frac * 20 + 0.999) - 1))
    return min(MAIN_MIN_CONC, CONC_Q[i])


def _main_skip_reason(card, before_info) -> str | None:
    """本予想の足切り(2026-10-22 ご主人様OK・ボートレース検証ラボの着眼点から)。
    8〜9月の本予想1,372Rで: 1号艇B級は66%、外の艇の展示が2位と0.06秒以上離れて1位は76%
    (全体81%)。両方を除くと約83〜84%。外の艇の展示が抜けると世間が飛びつきすぎて割高になる。"""
    b1 = next((r.get("racer_class") for r in (card or {}).get("racers", []) if r.get("lane") == 1), None)
    if b1 and b1.startswith("B"):
        return f"1号艇が{b1}"
    ex = sorted((b.get("exhibition_time"), b.get("lane")) for b in (before_info or [])
                if b.get("exhibition_time") is not None)
    if len(ex) >= 6 and ex[0][1] != 1 and ex[1][0] - ex[0][0] >= 0.06 - 1e-9:
        return f"{ex[0][1]}号艇の展示が{ex[1][0] - ex[0][0]:.2f}秒抜けている"
    return None


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
    # 江戸川 1-4-全 は 2026-10-21 停止(14-14-全に丸ごと含まれていて重複のため)
    ("v_edo1414", "03", "B", 1, 4, _perm([1, 4], [1, 4], [2, 3, 5, 6]), "江戸川 B級1〜4R 14-14-全"),  # 165% 3期113〜257% 上位3抜き106% ★
    ("v_suminoe", "12", "A1", 5, 8, ["1-2-3", "1-3-2"], "住之江 A1 5〜8R 1-23-23"),                   # 121% 3期116〜130% 上位3抜き100% ★
    ("v_maru", "15", "A1", 5, 8, _perm([1], [2], [3, 4, 5, 6]), "丸亀 A1 5〜8R 1-2-全"),              # 127% 上位3抜き95%
    ("v_heiwa", "04", "A1", 9, 12, ["1-2-3", "1-3-2"], "平和島 A1 9〜12R 1-23-23"),                   # 137% 上位3抜き99%
    ("v_fukuoka", "22", "A1", 5, 8, _perm([1], [2, 3, 4], [2, 3, 4]), "福岡 A1 5〜8R 1-234-234"),     # 131% 上位3抜き98%
    # 2026-10-23 ご主人様OK: 丸亀ナイター後半。1〜10月570R 的中率11.9% 回収127%(73/152/150%)・上位3本抜き98%
    ("v_maru456", "15", "", 9, 12, _perm([1], [4, 5, 6], [4, 5, 6]), "丸亀 9〜12R 1-456-456"),
    # 2026-10-20 ご主人様OK「宝探し枠」: 児島。的中が少なく偶然の可能性も高いので紙上で確かめる
    # 2026-10-23 13万R(2024/6〜)で3期とも175%超え・上位3本抜き122% → 主力に格上げ
    ("v_kojima456", "16", "B", 9, 12, _perm([1], [4, 5, 6], [4, 5, 6]), "★児島 B級 9〜12R 1-456-456"),
    # 2026-10-23 ご主人様OK: 13万Rで2024後半・2025・2026の3期すべて100%超えの残り4つ
    ("v_sumi2", "12", "A1", 5, 8, _perm([2], [1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6]), "住之江 A1 5〜8R 2-全-全"),      # 118%(102/110/141)
    ("v_kara2323", "23", "A2", 5, 8, _perm([2, 3], [2, 3], [1, 4, 5, 6]), "唐津 A2 5〜8R 23-23-全"),  # 154%(180/140/159)
    ("v_naru4", "14", "A1", 5, 8, _perm([4], [1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6]), "鳴門 A1 5〜8R 4-全-全"),        # 123%(125/123/122)
    ("v_biwa4", "11", "A1", 1, 4, _perm([4], [1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6]), "びわこ A1 1〜4R 4-全-全"),      # 165%(105/157/230)
    ("v_kojima516", "16", "", 1, 12, ["5-1-6"], "児島 全R 5-1-6"),                                   # 181% 3期144〜204% 的中16回
    # 停止(2026-10-20): 三国 3-1-全・浜名湖 23-23-全・唐津/桐生 123BOX は大当たり頼み or 期間でムラ
]
VERIFY_STADIUM_NAMES = {"15": "丸亀", "04": "平和島", "12": "住之江", "22": "福岡", "16": "児島", "23": "唐津", "14": "鳴門", "11": "びわこ"}


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


# 荒れ枠(2026-10-22 ご主人様OK): 全会場×風・波・流れを総当たりして、3期すべて100%超えはこれだけ。
# 三国・風5m以上で 23-1-全(8点): 1〜10月375R 回収112%(108/117/110%)・上位3本抜き87%。
WIND_RULES = [("v_mikuni_wind", "10", 5, _perm([2, 3], [1], [2, 3, 4, 5, 6]), "三国 風5m+ 23-1-全")]


# 天気枠(2026-10-22 ご主人様判断で追加): 大村・曇りの日 123BOX。1〜10月469R 回収137%(248/107/120%)・上位3本抜き107%。
# ただし大村の逃げ率は晴59%/曇60%/雨60%と天気で変わらず、偶然の可能性が高い(執事の見立て)。紙上で確かめる。
WEATHER_RULES = [("v_omura_cloud", "24", "曇り", _perm([1, 2, 3], [1, 2, 3], [1, 2, 3]), "大村 曇り 123BOX")]


def _run_wind_verify(state, target_date, session_name, stadium_code, race_number, odds_map, weather) -> None:
    wind = (weather or {}).get("wind")
    tenki = (weather or {}).get("weather")
    rules = [(bt, st, combos, title) for bt, st, mw, combos, title in WIND_RULES
             if st == stadium_code and wind is not None and wind >= mw]
    rules += [(bt, st, combos, title) for bt, st, tk, combos, title in WEATHER_RULES
              if st == stadium_code and tenki == tk]
    for bt, st, combos, title in rules:
        try:
            save_bet_tickets(state, [
                BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                          race_number=race_number, combination=c, amount=100, bet_type=bt,
                          odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
                for c in combos
            ])
            print(f"検証枠 {title}: {race_number}R 風{wind}m {tenki} 記録")
        except Exception as e:
            print(f"荒れ枠の保存失敗 {bt} {race_number}R: {e}")


# ===== 万舟検証枠(2026-10-23 ご主人様OK・紙上・1R100円) =====
# 万舟の典型は「1号艇より実力が上の外の艇が1着」。8〜9月の全国7,706Rで、100〜200倍の中から
#  A: 1号艇B級のとき、勝率1位の艇が頭の一番オッズが低い1点 → 2,794R 的中23本 回収96%(85/108%)
#  B: 勝率1位かつ今節一番好調の艇が1号艇以外のとき、その艇が頭の1点 → 1,719R 的中19本 回収126%(59/198%)
MANSHU_RULES = [("v_man_a", "万舟A 1号艇B級×勝率1位が頭"), ("v_man_b", "万舟B 勝率1位&今節好調1位が頭")]


def _manshu_pick(odds_map, head: int) -> tuple[str, float] | None:
    c = [(o, k) for k, o in (odds_map or {}).items() if o and 100 < o <= 200 and k[0] == head]
    if not c:
        return None
    o, k = min(c)
    return "-".join(map(str, k)), o


def _run_manshu_verify(state, target_date, session_name, stadium_code, race_number, odds_map, card) -> None:
    racers = {r.get("lane"): r for r in (card or {}).get("racers", [])}
    if len(racers) < 6 or not odds_map:
        return
    try:
        wr = {l: float(racers[l].get("national_win_rate") or 0) for l in range(1, 7)}
    except (TypeError, ValueError):
        return
    wr_top = max(wr, key=wr.get)
    picks = []
    b1 = racers[1].get("racer_class") or ""
    if b1.startswith("B") and wr_top != 1:
        picks.append(("v_man_a", wr_top))
    try:
        from src.tools.series_form import series_avg
        ser = series_avg(stadium_code, target_date)
        form = {l: ser.get(str(int(racers[l].get("number") or 0)), (0, 9.0)) for l in range(1, 7)}
        if all(n > 0 for n, _ in form.values()):
            best = min(form, key=lambda l: form[l][1])
            if best == wr_top and wr_top != 1:
                picks.append(("v_man_b", wr_top))
    except Exception as e:
        print(f"今節の調子の計算失敗: {e}")
    for bt, head in picks:
        p = _manshu_pick(odds_map, head)
        if not p:
            continue
        try:
            save_bet_tickets(state, [BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                                               race_number=race_number, combination=p[0], amount=100, bet_type=bt, odds=p[1])])
            print(f"万舟検証枠 {bt}: {race_number}R {p[0]} {p[1]}倍")
        except Exception as e:
            print(f"万舟検証枠の保存失敗 {bt}: {e}")


# 外枠の頭 検証枠(2026-10-24 ご主人様OK「5-1-全/6-1-全で調べて」「万舟の時の5・6号艇の有利な点」)。
# 13万R(2024/6〜2026/10)。() は 2024後半/2025/2026 の回収率。
OUTER_RULES = [
    ("v_5tilt", "全国 5号艇チルト伸び×当地1位×1号艇B級 5-1-全"),  # 456R 的中39 124%(106/107/150) 上位3抜き91%
    ("v_tsu61", "津 1号艇B級×6号艇平均ST1位 6-1-全"),              # 574R 的中31 259%(407/196/214) 上位3抜き134%
    ("v_biwa51", "びわこ 5号艇勝率2位内 5-1-全"),                   # 1608R 的中86 120%(137/122/107) 上位3抜き99%
    ("v_kojima51", "児島 5号艇A1×今節1位 5-1-全"),                  # 299R 的中24 131%(134/143/119) 上位3抜き94%
]


def _rank(vals: dict, lane: int, high: bool = True) -> int | None:
    """lane の順位(同値は上位扱い)。値がそろわなければ None。"""
    if any(v is None for v in vals.values()) or len(vals) < 6:
        return None
    v = vals[lane]
    return 1 + sum(1 for x in vals.values() if (x > v if high else x < v))


def _run_outer_verify(state, target_date, session_name, stadium_code, race_number, odds_map, card, before_info) -> None:
    racers = {r.get("lane"): r for r in (card or {}).get("racers", [])}
    if len(racers) < 6:
        return
    b1 = racers[1].get("racer_class") or ""
    g = lambda k: {l: racers[l].get(k) for l in range(1, 7)}
    bi = {b.get("lane"): b for b in (before_info or [])}
    picks = []
    tilt5 = (bi.get(5) or {}).get("tilt")
    ex = {l: (bi.get(l) or {}).get("exhibition_time") for l in range(1, 7)}
    if (b1.startswith("B") and tilt5 is not None and tilt5 >= 0.5 and _rank(g("local_win_rate"), 5) == 1
            and (_rank(g("national_win_rate"), 5) == 1 or _rank(ex, 5, high=False) == 1)):
        picks.append(("v_5tilt", 5))
    if stadium_code == "09" and b1.startswith("B") and _rank(g("avg_st"), 6, high=False) == 1:
        picks.append(("v_tsu61", 6))
    if stadium_code == "11" and (_rank(g("national_win_rate"), 5) or 9) <= 2:
        picks.append(("v_biwa51", 5))
    if stadium_code == "16" and racers[5].get("racer_class") == "A1":
        try:
            from src.tools.series_form import series_avg
            ser = series_avg(stadium_code, target_date)
            form = {l: ser.get(str(int(racers[l].get("number") or 0)), (0, None))[1] for l in range(1, 7)}
            if _rank(form, 5, high=False) == 1:
                picks.append(("v_kojima51", 5))
        except Exception as e:
            print(f"今節の調子の計算失敗: {e}")
    for bt, head in picks:
        combos = [f"{head}-1-{c}" for c in range(2, 7) if c != head]
        try:
            save_bet_tickets(state, [
                BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                          race_number=race_number, combination=c, amount=100, bet_type=bt,
                          odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
                for c in combos])
            print(f"外枠の頭 検証枠 {bt}: {race_number}R {head}-1-全")
        except Exception as e:
            print(f"外枠の頭 検証枠の保存失敗 {bt}: {e}")


def _run_exp2(state, target_date, session_name, stadium_code, race_number, odds_map, card, before_info, weather) -> None:
    """実験枠②(2026-10-26 ご主人様OK): 世間の見落とし補正AI。割安度0.9以上・100倍未満を1点100円で紙上記録。
    答えを見ていない2026年7〜10月の確定オッズでは回収率約90%(全部買うと77%)。本番は締切前オッズなので少し下がる見込み。"""
    try:
        from src.models.adjust import boat_features, value_picks
        racers = {r.get("lane"): r for r in (card or {}).get("racers", [])}
        bi = {b.get("lane"): b for b in (before_info or [])}
        if len(racers) < 6 or not odds_map:
            return
        rows = []
        for l in range(1, 7):
            r, b = racers[l], bi.get(l) or {}
            ex = b.get("exhibition_time")
            rows.append({"wr": r.get("national_win_rate"), "top2": r.get("top2"), "local": r.get("local_win_rate"),
                         "motor": r.get("motor_2rate"), "boat": r.get("boat_2rate"), "ex": ex if ex and ex > 0 else None,
                         "avgst": r.get("avg_st"), "pvst": b.get("start_timing"), "tilt": b.get("tilt"),
                         "age": r.get("age"), "weight": r.get("weight"), "cls": r.get("racer_class"),
                         "F": r.get("flying"), "course": b.get("course")})
        feats = boat_features(rows, wind=(weather or {}).get("wind"), wave=(weather or {}).get("wave"))
        odds = {"-".join(map(str, k)): float(o) for k, o in odds_map.items() if o}
        picks = value_picks(odds, feats)
        if not picks:
            return
        save_bet_tickets(state, [BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                                           race_number=race_number, combination=c, amount=100, bet_type="exp2", odds=o)
                                 for c, o, _v in picks])
        print(f"実験枠② {race_number}R {len(picks)}点: " + " ".join(f"{c}({o}倍)" for c, o, _ in picks[:6]))
    except Exception as e:
        print(f"実験枠②の失敗 {race_number}R: {e}")


def _run_tanfuku(target_date, session_name, stadium_code, race_number, before_info) -> None:
    """1号艇の単勝・複勝 検証枠(2026-10-30 ご主人様「1コース1号艇の複勝はとことんやってみな」)。
    締切前の単勝・複勝オッズを plans/tanfuku/日付.csv に記録し、翌朝の答え合わせで採点する。
    確定オッズ(2026/4〜10・展示1コースの1号艇2.9万R)では毎月100%超え:
      A 単勝3.0倍以上の1号艇の単勝   4,242R 116%(上位10本抜き112%)
      B 複勝(下限)1.4倍以上の1号艇の複勝 5,783R 113%(上位10本抜き109%)
    締切前オッズでも同じになるかを確かめる。"""
    try:
        bi = {b.get("lane"): b for b in (before_info or [])}
        course1 = (bi.get(1) or {}).get("course")
        from src.tools.collect_odds_other import _get, parse_tf
        od = parse_tf(_get("oddstf", stadium_code, race_number, target_date))
        w, pl = od.get("win_1"), od.get("place_1")
        if w is None or pl is None:
            return
        os.makedirs(os.path.join("plans", "tanfuku"), exist_ok=True)
        path = os.path.join("plans", "tanfuku", f"{target_date}.csv")
        new_file = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if new_file:
                f.write("session,stadium_code,race_number,course1,win_1,place_1,time\n")
            f.write(f"{session_name},{stadium_code},{race_number},{course1 if course1 else ''},{w},{pl},"
                    f"{datetime.now(JST):%H:%M}\n")
        print(f"単複記録 {stadium_code} {race_number}R 1号艇 単{w} 複{pl} 進入{course1}")
    except Exception as e:
        print(f"単複記録の失敗 {race_number}R: {e}")


# 当たりやすいレースだけ枠(2026-10-30 ご主人様「当たりやすいレースに絞ろう」)。
# オッズから出した「その買い方が当たる確率」が高いレースだけ買う。確定オッズ16か月(7.6万R)での回収率:
#   123BOX    当たる確率24%以上(上位約20%)  約80%(全レースだと64%)
#   1-456-456 当たる確率12%以上(上位約20%)  約75〜80%(全レースだと58%)
LIKELY_RULES = [("v_123hi", _perm([1, 2, 3], [1, 2, 3], [1, 2, 3]), 0.24, "当たりやすい 123BOX(確率24%以上)"),
                ("v_456hi", _perm([1], [4, 5, 6], [4, 5, 6]), 0.12, "当たりやすい 1-456-456(確率12%以上)")]


def _run_likely(state, target_date, session_name, stadium_code, race_number, odds_map) -> None:
    inv = {k: 1 / o for k, o in (odds_map or {}).items() if o and o > 0}
    tot = sum(inv.values())
    if not tot:
        return
    for bt, combos, th, title in LIKELY_RULES:
        p = sum(inv.get(tuple(map(int, c.split("-"))), 0) for c in combos) / tot
        if p < th:
            continue
        try:
            save_bet_tickets(state, [BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                                               race_number=race_number, combination=c, amount=100, bet_type=bt,
                                               odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
                                     for c in combos])
            print(f"{title}: {race_number}R 確率{p * 100:.0f}%")
        except Exception as e:
            print(f"当たりやすい枠の保存失敗 {bt}: {e}")


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
        done_n = _main_count(state, target_date, session_name)
        need = MAIN_TARGET_PER_SESSION - done_n
        remaining = sum(1 for sn, r in todo if sn == session_name
                        and (r.get("deadline") or "99:99") >= (race.get("deadline") or "99:99"))
        thr = _quota_threshold(need, remaining)
        pop_plans, conc = build_popular_plan(odds_map, min_concentration=min(thr, 1.0))
        print(f"{label}: 集中度{conc * 100:.1f}% (本予想{done_n}/{MAIN_TARGET_PER_SESSION}R・残り{remaining}R・下限{thr * 100:.0f}%)")
        skip = _main_skip_reason(card, before_info)
        if skip and remaining <= need * 1.5:
            skip = None  # 10Rに届かなくなりそうな時は足切りしない
        if pop_plans and skip:
            print(f"{label}: 本予想は見送り({skip})")
            pop_plans = []
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
        _run_wind_verify(state, target_date, session_name, stadium_code, race_number, odds_map, weather)
        _run_manshu_verify(state, target_date, session_name, stadium_code, race_number, odds_map, card)
        _run_outer_verify(state, target_date, session_name, stadium_code, race_number, odds_map, card, before_info)
        _run_exp2(state, target_date, session_name, stadium_code, race_number, odds_map, card, before_info, weather)
        _run_tanfuku(target_date, session_name, stadium_code, race_number, before_info)
        _run_likely(state, target_date, session_name, stadium_code, race_number, odds_map)

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
