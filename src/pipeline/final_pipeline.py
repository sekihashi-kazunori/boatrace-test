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

from src.collectors.official_site import CANCELLED, fetch_race_card, fetch_before_info, fetch_odds_3rentan, LAST_WEATHER, fetch_race_deadline_times, _classify_session_from_first_deadline
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
        # 2026-11-06: 1-456-456 の部分は停止(新システムの v_1456b で検証)
        return [BetPlan(c, "江戸川鉄板", 200, 0.0, _odds(odds_map, c), "1号艇が逃げて2・3号艇が続く形")
                for c in ("1-2-3", "1-3-2")]
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
# 2026-11-04 ご主人様判断で元に戻す: 10R化(10/30〜)で回収率が90%→58%に落ちたため。
# 集中度0.45以上だけ・各セッション最大5R(1日5〜12R程度)
MAIN_MAX_PER_SESSION = 5
MAIN_TARGET_PER_SESSION = 0
# 2026-11-05〜 ご主人様指示: モーニング/デイ/ナイターで分けず、1日を通して自信のあるレースだけ最大10R。
# 締切前オッズの集中度(上位5点)0.47以上 ≒ 全レースの上位5%(1日7〜10R)。セッションごとの上限はなし。
MAIN_MIN_CONC_NEW = 0.47
TRIO_AMT = 400  # 2026-11-21 本予想の3連複1点の掛け金(3連単は2点で200円)
MAIN_MAX_PER_DAY = 20  # 2026-11-21 ご主人様「最低15レース」: 自信Bを足して上限20Rに。旧: 2026-11-16 ご主人様「1日15レース迄に拡大」(11/14・15は16:21で10Rを使い切りナイター0R)
MAIN_EXTRA_MAX = 15  # 10Rを超えたら自信S/Aだけ、15Rまで
MAIN_STOP_DATE = "2099-01-01"  # 2026-11-08 ご主人様指示で本予想(3連単)を復活(11/6〜11/7は停止していた)
# 締切前オッズの集中度(上位5点)の分布(10/11〜10/29の2,419R)。CONC_Q[i] より上のレースが全体の (i+1)*5%
CONC_Q = [0.466, 0.429, 0.403, 0.383, 0.366, 0.35, 0.337, 0.325, 0.311, 0.297, 0.284, 0.273, 0.261, 0.248,
          0.235, 0.222, 0.21, 0.193, 0.172]


def _quota_threshold(need: int, remaining: int) -> float:
    """残りレースの中から need レース選ぶための集中度の下限。
    集中度0.45以上はいつでも買う。足りなさそうなら、残りの上位 need/remaining の割合まで下げる。"""
    if MAIN_TARGET_PER_SESSION <= 0:
        return MAIN_MIN_CONC  # 10R化をやめた(2026-11-04): 鉄板(0.45以上)だけ
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


def _main_count_session(state, target_date, session_name) -> int:
    with OrmSession(state) as db:
        rows = db.execute(select(BetTicket.stadium_code, BetTicket.race_number).where(
            BetTicket.race_date == str(target_date), BetTicket.session == session_name,
            BetTicket.bet_type == "gen").distinct()).all()
    return len(rows)


def _main_count(state, target_date, session_name=None) -> int:
    """その日に本予想(gen)を買ったレース数(2026-11-05〜 セッションは問わず1日の合計)。"""
    with OrmSession(state) as db:
        rows = db.execute(select(BetTicket.stadium_code, BetTicket.race_number).where(
            BetTicket.race_date == str(target_date), BetTicket.bet_type == "gen").distinct()).all()
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
    # v_maru456(丸亀 9〜12R 1-456-456) は 2026-11-06 停止(新システムへ)
    # 2026-10-20 ご主人様OK「宝探し枠」: 児島。的中が少なく偶然の可能性も高いので紙上で確かめる
    # 2026-10-23 13万R(2024/6〜)で3期とも175%超え・上位3本抜き122% → 主力に格上げ
    # v_kojima456(児島 B級 9〜12R 1-456-456) は 2026-11-06 停止(新システム v_1456b に児島も含む)
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
WEATHER_RULES = []  # 2026-11-03 ご主人様判断で123BOXは全部停止(大村 曇り 123BOX も)


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
MANSHU_RULES = []  # 2026-11-06 廃止(旧: v_man_a 万舟A・v_man_b 万舟B)


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


def _tanfuku_tier(card, before_info, win_1) -> str:
    """2026-11-02 ご主人様「月90万で30万の利益」: リーサルウェポンA(単勝3倍以上)を3段に分ける。
    確定オッズ17か月・前半/後半: S 146%/153%(1日7R) / B 108%/109%(8R) / C 94%/88%(6R)
      S: 1号艇A級 or 全国勝率2位以内 or 当地勝率2位以内 or 単勝6倍以上
      B: Sでなく、1号艇の展示タイム3位以内
      C: それ以外"""
    if win_1 is None or win_1 < 3:
        return ""
    racers = {r.get("lane"): r for r in (card or {}).get("racers", [])}
    r1 = racers.get(1) or {}

    def rank(key, src):
        v = {l: (src.get(l) or {}).get(key) for l in range(1, 7)}
        if v[1] is None or any(x is None for x in v.values()):
            return 9
        hi = key != "exhibition_time"
        return 1 + sum(1 for x in v.values() if (x > v[1] if hi else x < v[1]))
    cls = r1.get("racer_class") or ""
    if cls.startswith("A") or win_1 >= 6 or rank("national_win_rate", racers) <= 2 or rank("local_win_rate", racers) <= 2:
        return "S"
    bi = {b.get("lane"): b for b in (before_info or [])}
    return "B" if rank("exhibition_time", bi) <= 3 else "C"


def _run_tanfuku(target_date, session_name, stadium_code, race_number, before_info, card=None, deadline=None) -> None:
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
        # 2026-11-06: 締切前の6艇の単勝オッズを全部残す(確定オッズで作ったAIを、締切前オッズで正しく試すため)
        try:
            path2 = os.path.join("plans", "tanodds", f"{target_date}.csv")
            os.makedirs(os.path.dirname(path2), exist_ok=True)
            new2 = not os.path.exists(path2)
            with open(path2, "a", encoding="utf-8") as f:
                if new2:
                    f.write("stadium_code,race_number,time,deadline," + ",".join(f"win_{i}" for i in range(1, 7)) + ","
                            + ",".join(f"place_{i}" for i in range(1, 7)) + "\n")
                f.write(f"{stadium_code},{race_number},{datetime.now(JST):%H:%M},{deadline or ''},"
                        + ",".join(str(od.get(f"win_{i}", "")) for i in range(1, 7)) + ","
                        + ",".join(str(od.get(f"place_{i}", "")) for i in range(1, 7)) + "\n")
        except Exception as e:
            print(f"単勝6艇記録の失敗 {race_number}R: {e}")
        if w is None or pl is None:
            return
        os.makedirs(os.path.join("plans", "tanfuku"), exist_ok=True)
        path = os.path.join("plans", "tanfuku", f"{target_date}.csv")
        head = "session,stadium_code,race_number,course1,win_1,place_1,time,tier,pool,stake\n"
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                txt = f.read()
            if not txt.startswith(head):  # 2026-11-06 列追加前のファイルは見出しだけ直す
                body = txt.split("\n", 1)[1] if "\n" in txt else ""
                with open(path, "w", encoding="utf-8") as f:
                    f.write(head + body)
        new_file = not os.path.exists(path)
        tier = _tanfuku_tier(card, before_info, w)
        # 2026-11-06 ご主人様OK: 売上に合わせて賭ける(S 3%・B 1%)。締切前オッズから売上を推定して額を決める
        pool = stake_amt = ""
        if tier in ("S", "B"):
            from src.tools.tan_pool import estimate_pool, stake
            p = estimate_pool(od)
            pool, stake_amt = (p or ""), stake(tier, p)
        with open(path, "a", encoding="utf-8") as f:
            if new_file:
                f.write(head)
            f.write(f"{session_name},{stadium_code},{race_number},{course1 if course1 else ''},{w},{pl},"
                    f"{datetime.now(JST):%H:%M},{tier},{pool},{stake_amt}\n")
        print(f"単複記録 {stadium_code} {race_number}R 1号艇 単{w} 複{pl} 進入{course1} {tier} 売上{pool} 賭け{stake_amt}")
    except Exception as e:
        print(f"単複記録の失敗 {race_number}R: {e}")


# 当たりやすいレースだけ枠(2026-10-30 ご主人様「当たりやすいレースに絞ろう」)。
# オッズから出した「その買い方が当たる確率」が高いレースだけ買う。確定オッズ16か月(7.6万R)での回収率:
#   123BOX    当たる確率24%以上(上位約20%)  約80%(全レースだと64%)
#   1-456-456 当たる確率12%以上(上位約20%)  約75〜80%(全レースだと58%)
# 2026-11-03 ご主人様判断で 当たりやすい123BOX は停止
# 2026-11-06 停止: v_456hi(当たりやすい 1-456-456)。新システム v_1456b へ
LIKELY_RULES = []


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


MAIN_BAD_STADIUMS = {"10", "14"}  # 2026-11-05〜 三国・鳴門は鉄板に見えても荒れやすい(集中度0.47以上の人気4点 65〜78%)
MAIN_MIN_VALUE = 0.80             # 見落とし補正AIの割安度がこれ未満の本命は外す


def _adj_values(odds_map, card, before_info, weather) -> dict:
    """見落とし補正AIで {"1-2-3": 割安度(補正確率×オッズ)}。失敗したら {}。"""
    try:
        from src.models.adjust import boat_features, adjusted_probs
        racers = {r.get("lane"): r for r in (card or {}).get("racers", [])}
        bi = {b.get("lane"): b for b in (before_info or [])}
        if len(racers) < 6 or not odds_map:
            return {}
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
        p = adjusted_probs(odds, feats)
        return {k: p[k] * odds[k] for k in p}
    except Exception as e:
        print(f"割安度の計算失敗: {e}")
        return {}


def _filter_main_by_value(plans, values, label):
    """本命のうち割高(割安度<MAIN_MIN_VALUE)を外し、残りで900円をノーガミ配分し直す。本命は最低2点残す。"""
    if not values:
        return plans
    from src.betting.allocation import _no_gami_stakes, _inverse_stakes
    main = [p for p in plans if p.category == "本命"]
    other = [p for p in plans if p.category != "本命"]
    keep = [p for p in main if values.get(p.combination, 1.0) >= MAIN_MIN_VALUE]
    if len(keep) < 2:
        keep = sorted(main, key=lambda p: -values.get(p.combination, 0))[:2]
    if len(keep) == len(main):
        return plans
    dropped = [f"{p.combination}({values.get(p.combination, 0):.2f})" for p in main if p not in keep]
    st = _no_gami_stakes([p.odds for p in keep], 900, target=1100) or _inverse_stakes([p.odds for p in keep], 900)
    keep = keep[:len(st)]
    for p, a in zip(keep, st):
        p.stake = a
    print(f"{label}: 割高な本命を外す {' '.join(dropped)}")
    return keep + other


# 2026-11-06 ご主人様OK: 456ボックスを1,270条件で掘り下げ、前後の期間で両方100%超えだった唯一の条件(偶然の可能性あり)。
# 芦屋・宮島・戸田 × 1号艇B級 × 風3m以上 → 456BOX 6点。確定オッズ 25/6〜26/1 111% / 26/2〜10 103%(各約20本的中)。
BOX456_RULES = [("v_456box", "芦屋宮島戸田×1号艇B級×風3m+ 456BOX")]


def _run_456box(state, target_date, session_name, stadium_code, race_number, odds_map, card) -> None:
    if stadium_code not in ("21", "17", "02"):
        return
    r1 = next((r for r in (card or {}).get("racers", []) if r.get("lane") == 1), {})
    if not (r1.get("racer_class") or "").startswith("B"):
        return
    wind = LAST_WEATHER.get((stadium_code, race_number, str(target_date)), {}).get("wind")
    if wind is None or wind < 3:
        return
    combos = _perm([4, 5, 6], [4, 5, 6], [4, 5, 6])
    try:
        save_bet_tickets(state, [BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                                           race_number=race_number, combination=c, amount=100, bet_type="v_456box",
                                           odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
                                 for c in combos])
        print(f"456BOX検証枠: {race_number}R 風{wind}m")
    except Exception as e:
        print(f"456BOX検証枠の保存失敗: {e}")


# 2026-11-21 ご主人様「129と456は執事君の采配に任せる！全レースでは無く会場とレースを読んで掛けてみて」:
# 2026年1〜10月 約4万Rで条件を探し、前半(1〜5月)・後半(6〜10月)の両方で確かめた形だけ(紙上の検証枠)。
#  v_456sel: 9〜12R × 1号艇の全国勝率5.0以下 × 4・5・6号艇のどれかが展示タイム2位以内 → 456BOX 6点×100円
#            960R 的中24本 回収率 前132%・後188%(9か月中7か月プラス)。ただし大きい3本を除くと83%で、波が大きい。
#  v_12sel : 9〜12R × 1号艇の全国勝率6.5以上・展示タイム2位以内 × 2号艇の全国勝率が3〜6号艇より上 → 1-2-全 4点×100円
#            736R 的中289本 回収率 前91%・後96%(全レース平均75%より上。まだ100%には届かない)
SEL_RULES = [("v_456sel", "9〜12R×1号艇勝率5.0以下×456展示2位内 456BOX"),
             ("v_12sel", "9〜12R×1号艇勝率6.5以上展示2位内×2号艇が2番手 1-2-全")]


def _run_sel(state, target_date, session_name, stadium_code, race_number, odds_map, card, before_info) -> None:
    if not (9 <= race_number <= 12):
        return
    racers = {r.get("lane"): r for r in (card or {}).get("racers", [])}
    bi = {b.get("lane"): b for b in (before_info or [])}
    try:
        nw = {l: float(racers[l].get("national_win_rate")) for l in range(1, 7)}
        ex = {l: float((bi.get(l) or {}).get("exhibition_time")) for l in range(1, 7)}
    except (KeyError, TypeError, ValueError):
        return
    order = sorted(range(1, 7), key=lambda l: ex[l])
    rk = {l: order.index(l) + 1 for l in range(1, 7)}
    picks = []
    # 2026-11-23 ご主人様「456はやめて良い！」: v_456sel は停止(11/21〜23 12R 回収14%)。1-2-全(v_12sel)だけ続ける
    if nw[1] >= 6.5 and rk[1] <= 2 and nw[2] >= max(nw[3], nw[4], nw[5], nw[6]):
        picks.append(("v_12sel", [f"1-2-{c}" for c in range(3, 7)]))
    for bt, combos in picks:
        try:
            save_bet_tickets(state, [BetTicket(race_date=str(target_date), session=session_name,
                                               stadium_code=stadium_code, race_number=race_number, combination=c,
                                               amount=100, bet_type=bt,
                                               odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
                                     for c in combos])
            print(f"執事の采配 {bt}: {stadium_code} {race_number}R " + " ".join(combos))
        except Exception as e:
            print(f"執事の采配 {bt} の保存失敗: {e}")


# 2026-11-06 ご主人様OK: 1-456-456 の新システム。会場×級×R帯を掘り下げ、前後の期間で両方100%超え(128% / 124%)。
# 江戸川・児島・津・尼崎・宮島・戸田 × 1号艇B級 × 9〜12R → 1-456-456 6点×100円。
BOX1456_RULES = [("v_1456b", "江戸川児島津尼崎宮島戸田×1号艇B級×9〜12R 1-456-456")]


def _run_1456b(state, target_date, session_name, stadium_code, race_number, odds_map, card) -> None:
    if stadium_code not in ("03", "16", "09", "13", "17", "02") or not (9 <= race_number <= 12):
        return
    r1 = next((r for r in (card or {}).get("racers", []) if r.get("lane") == 1), {})
    if not (r1.get("racer_class") or "").startswith("B"):
        return
    combos = _perm([1], [4, 5, 6], [4, 5, 6])
    try:
        save_bet_tickets(state, [BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                                           race_number=race_number, combination=c, amount=100, bet_type="v_1456b",
                                           odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
                                 for c in combos])
        print(f"1-456-456新枠: {stadium_code} {race_number}R")
    except Exception as e:
        print(f"1-456-456新枠の保存失敗: {e}")


# 2026-11-06 ご主人様OK: 江戸川 × 1号艇の展示ST4位以下 × 1-5-全(4点)。
# 生データ29か月で 探す期間(〜2025) 118% / 答えを見ていない2026年 134%(上位3本抜き101%)。展示ST1〜3位だと50〜74%。
EDO15_RULES = [("v_edo15", "江戸川×1号艇の展示ST4位以下 1-5-全")]


def _run_edo15(state, target_date, session_name, stadium_code, race_number, odds_map, before_info) -> None:
    if stadium_code != "03":
        return
    bi = {b.get("lane"): b for b in (before_info or [])}
    st = {l: (bi.get(l) or {}).get("start_timing") for l in range(1, 7)}
    if any(v is None for v in st.values()) or (bi.get(1) or {}).get("course") not in (1, None):
        return
    rank1 = 1 + sum(1 for l in range(2, 7) if st[l] < st[1])
    if rank1 < 4:
        return
    combos = [f"1-5-{k}" for k in (2, 3, 4, 6)]
    try:
        save_bet_tickets(state, [BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                                           race_number=race_number, combination=c, amount=100, bet_type="v_edo15",
                                           odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
                                 for c in combos])
        print(f"江戸川1-5-全枠: {race_number}R 1号艇展示ST{rank1}位")
    except Exception as e:
        print(f"江戸川1-5-全枠の保存失敗: {e}")


# 2026-11-06 ご主人様OK「別会場でも何か潜んでないか」: 全24場・14万通りを 探す期間(〜2025)→答えを見ていない2026年 で試した残り。
#   鳴門 風1m以下 × 1号艇の全国勝率2位以内 → 5-1-全: 探す248% / 2026年 154R 243%(的中8本)
#   三国 風6m以上 → 3-1-全: 探す172% / 2026年 219R 102%(的中12本)
VENUE_RULES = [("v_nar51", "鳴門×風1m以下×1号艇勝率2位内 5-1-全"), ("v_mik31", "三国×風6m以上 3-1-全")]


def _run_venue(state, target_date, session_name, stadium_code, race_number, odds_map, card, weather) -> None:
    wind = (weather or {}).get("wind")
    if wind is None:
        return
    plans = []
    if stadium_code == "14" and wind <= 1:
        rates = {r.get("lane"): r.get("national_win_rate") for r in (card or {}).get("racers", [])}
        if all(rates.get(l) is not None for l in range(1, 7)) and 1 + sum(1 for l in range(2, 7) if rates[l] > rates[1]) <= 2:
            plans.append(("v_nar51", [f"5-1-{k}" for k in (2, 3, 4, 6)]))
    if stadium_code == "10" and wind >= 6:
        plans.append(("v_mik31", [f"3-1-{k}" for k in (2, 4, 5, 6)]))
    for bt, combos in plans:
        try:
            save_bet_tickets(state, [BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                                               race_number=race_number, combination=c, amount=100, bet_type=bt,
                                               odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
                                     for c in combos])
            print(f"会場別新枠 {bt}: {stadium_code} {race_number}R 風{wind}m")
        except Exception as e:
            print(f"会場別新枠の保存失敗 {bt}: {e}")


# 2026-11-08 ご主人様指示「1からやり直し・的中率を上げろ」: オッズを使わないAIの3連単1点(紙上)。
# 2026年1〜10月(答えを見ていない4.7万R)で 自信上位5% 的中率17.6%・回収87% / 上位2% 19.6%・89%。
AI3_RULES = [("ai3", "執事の本予想(レースの中身から・自信上位5%) 3連単 鉄板6点+穴1点・1R約1,500円")]


def _run_ai3(state, target_date, session_name, stadium_code, race_number, odds_map, card, before_info, weather,
             stadium_name="", force=False, deadline=None) -> None:
    try:
        from src.models import ai3
        racers = {r.get("lane"): r for r in (card or {}).get("racers", [])}
        bi = {b.get("lane"): b for b in (before_info or [])}
        if len(racers) < 6:
            return
        rows = []
        for l in range(1, 7):
            r, b = racers.get(l) or {}, bi.get(l) or {}
            rows.append({"wr": r.get("national_win_rate"), "top2": r.get("top2"), "local": r.get("local_win_rate"),
                         "motor": r.get("motor_2rate"), "boat": r.get("boat_2rate"), "ex": b.get("exhibition_time"),
                         "avgst": r.get("avg_st"), "pvst": b.get("start_timing"), "tilt": b.get("tilt"),
                         "age": r.get("age"), "weight": r.get("weight"), "cls": r.get("racer_class"),
                         "F": r.get("flying"), "course": b.get("course")})
        odds_s = {"-".join(map(str, k)): float(o) for k, o in (odds_map or {}).items() if o}
        try:
            from src.tools.female_racers import is_ladies
            ladies = is_ladies([racers.get(l, {}).get("number") for l in range(1, 7)])
        except Exception as e:
            print(f"レディース判定の失敗: {e}")
            ladies = False
        _run_are(state, target_date, session_name, stadium_code, race_number, odds_map, rows, weather, stadium_name,
                 deadline)
        _run_watch(state, target_date, session_name, stadium_code, race_number, odds_map, rows, weather, stadium_name,
                   deadline)
        _run_minamo(state, target_date, session_name, stadium_code, race_number, odds_map, stadium_name, deadline)
        # 2026-11-13 ご主人様「水面は大事。こまめに見ろ」: その日その場で1号艇が4R以上走って6割以上勝っている本命水面は、
        # 次のレースも1号艇が勝ちやすい(2025〜26年 約10万R: 1号艇の勝率 全体54.6% → 本命水面60〜63%、荒れ水面〜25%は50%)。
        # 本命水面では自信Aの下限をレディースと同じだけ下げて、拾えるレースを増やす
        honmei = False
        try:
            from src.tools.day_trend import boat1_lose_rate
            _np, _lo = boat1_lose_rate(stadium_code, int(race_number), target_date)
            honmei = _np >= 4 and _lo is not None and _lo <= 0.4
            if honmei:
                print(f"執事の本予想 {stadium_name}{race_number}R: 本命水面(1号艇{_np}R中{round(_np * (1 - _lo))}勝)")
        except Exception as e:
            print(f"本命水面の判定失敗: {e}")
        res = ai3.pick(rows, int(stadium_code), int(race_number), (weather or {}).get("wind"), (weather or {}).get("wave"),
                       n=6, odds=odds_s, ladies=ladies or honmei)  # 2026-11-14 ご主人様OK「6点で良い」: 8点→6点(ガミ対策)
        if not res:
            return
        top6, tier, ana = res
        print(f"執事の本予想 {stadium_name}{race_number}R: 一番の買い目{top6[0][0]} 確率{top6[0][1] * 100:.1f}% 級{tier} 本予想レース{force}")
        _save_tier(target_date, stadium_code, race_number, tier, top6[0][0], top6[0][1])
        _run_kaigi(state, target_date, session_name, stadium_code, race_number, odds_map, top6, stadium_name)
        if not tier and not force:
            return
        # 2026-11-17 ご主人様「みなもさんの予想を加味しなさい」: みなもさんの◎が1号艇以外のレースは
        # 1号艇の勝率17%・万舟33%(11/14〜16の52R。◎が1号艇なら1号艇64%)。1号艇頭の本予想は見送る
        mk = minamo_mark(target_date, stadium_code, race_number)
        if mk and len(mk) > 1 and mk[1] != "1" and top6[0][0][0] == "1":
            print(f"執事の本予想 {stadium_name}{race_number}R: みなもさんの◎が{mk[1]}号艇(1号艇以外)なので見送り")
            return
        # 2026-11-10 場の掘り下げ: 自信S・Aの6点的中率が戸田50%・鳴門53%と低い(全場61%)。この2場は自信Sだけにする
        if stadium_code in ("02", "14") and tier != "S":
            print(f"執事の本予想 {stadium_name}{race_number}R: 戸田・鳴門は自信Sだけ(今は自信{tier})")
            return
        # 2026-11-10 掘り下げ(反省): その日その場で前のレースの1号艇が4R以上走って75%以上負けている水面は、
        # 執事の読みより1号艇が4.5pt弱い(風4m以上なら5.2pt)。11/10の宮島・福岡がこれ。本予想は見送る
        try:
            from src.tools.day_trend import boat1_lose_rate
            n_prev, lose = boat1_lose_rate(stadium_code, int(race_number), target_date)
            if n_prev >= 4 and lose is not None and lose >= 0.75:
                print(f"執事の本予想 {stadium_name}{race_number}R: 今日のこの場は1号艇が{n_prev}R中{lose * 100:.0f}%負けている荒れ水面なので見送り")
                return
        except Exception as e:
            print(f"その日の流れの計算失敗: {e}")
        # 2026-11-10 掘り下げ: 自信S/Aでも風5m以上は 6点的中率 57%→53%・回収77%以下に落ちる(2026年3,251R)。見送る
        wind = (weather or {}).get("wind")
        if wind is not None and float(wind) >= 5:
            print(f"執事の本予想 {stadium_name}{race_number}R: 風{wind}mで見送り(風5m以上は1号艇が崩れやすい)")
            return
        n_done = _main_count(state, target_date)
        # 2026-11-13 反省: 10Rの枠を18:33で使い切り、夜の本命水面(蒲郡11R・大村11R、湊さんは的中)を買えなかった。
        # 15時までは5Rまでにして、残りの枠は水面の様子が見えてくる夕方〜夜のために残す
        _now = datetime.now(ZoneInfo("Asia/Tokyo"))
        # 2026-11-16: 1日15Rに。15時までは5R・17時までは10Rまでにして、17時からのナイターに最低5R残す
        # 2026-11-21: 1日20Rまでに合わせて 15時まで7R・17時まで14R(ナイターに最低6R残す)
        if n_done >= 7 and _now.hour < 15:
            print(f"執事の本予想 {stadium_name}{race_number}R: 15時までの枠(7R)を使い切ったので見送り(夜の本命水面のために残す)")
            return
        if n_done >= 14 and _now.hour < 17:
            print(f"執事の本予想 {stadium_name}{race_number}R: 17時までの枠(14R)を使い切ったので見送り(ナイターの6Rのために残す)")
            return
        if n_done >= MAIN_MAX_PER_DAY:
            print(f"執事の本予想: 今日はすでに{n_done}R出したので見送り(1日20Rまで・自信S/A/B)")
            return
        # 2026-11-14 ご主人様OK「6点で良いよ」: ガミが多すぎて回収率が上がらない反省。
        # 実戦40R(11/11〜14)で 今の8〜10点(1番手厚め+穴+逆転の穴) 的中57%・回収69% → 上位6点均等 的中52%・回収85%。
        # 2026年 自信S/A 3,253Rでは 6点 的中61%・回収82%。穴・逆転の穴・ガミ対策の追加はやめ、上位6点を200円ずつ(1,200円)
        ana, ana2 = None, None
        picks = list(top6)
        # 2026-11-15 ご主人様「鉄板1-2-4の逆張り2-1-4を入れてない。当たってたのに」(津5R 2-1-4 7,530円):
        # 1番手の頭と2着を入れ替えた「逆転」を、もう1点100円で必ず押さえる(合計1,200円の中で)。
        # 11/11〜15の実戦45Rで逆転は3回的中(宮島1R 3-1-2 15,740円・尼崎12R 3-1-5・津5R 2-1-4)
        a_, b_, c_ = top6[0][0].split("-")
        gyaku = f"{b_}-{a_}-{c_}"
        has_g = any(gyaku == c for c, _q in picks)
        # 2026-11-16 ご主人様OK「プラスしても良いよ」: 1号艇頭なのに「1-4-◯」が1点も無い時は、1-4-(一番来そうな3着)を100円。
        # 6日61Rの反省: 1号艇が勝って外した15Rのうち6Rは2着4号艇の抜け。実戦33Rで足していれば3回的中(+6,400円)
        yon = None
        pp = {}
        if a_ == "1":
            pp = ai3.probs(rows, int(stadium_code), int(race_number), (weather or {}).get("wind"),
                           (weather or {}).get("wave")) or {}
        if a_ == "1" and not any(c.startswith("1-4-") for c, _q in picks):
            c14 = [c for c in pp if c.startswith("1-4-")]
            if c14:
                yon = max(c14, key=lambda c: pp[c])
                if yon == gyaku:
                    yon = None
        # 2026-11-17 ご主人様「こう言うの取りなよ!」(唐津2R 1-6-2 45,120円): 1号艇頭なら「1-5-◯ / 1-6-◯」の
        # 一番来そうな目を万舟の押さえとして100円。6日61Rで1-5/1-6の2着で外したのが4本(計14,980円・費用6,100円)
        man = None
        if a_ == "1":
            have = {c for c, _q in picks} | {gyaku, yon}
            c56 = [c for c in pp if c[:3] in ("1-5", "1-6") and c not in have]
            if c56:
                man = max(c56, key=lambda c: pp[c])
        extra = (0 if has_g else 100) + (100 if yon else 0) + (100 if man else 0)
        # 2026-11-15 ご主人様「24.3倍に200円はおかしい」: 安い目に厚く・高い目は100円
        # 2026-11-17 ご主人様OK: 鉄板6点は1,200円のまま、押さえ(逆転・4号艇2着・万舟)は100円ずつ上乗せ(最大1,500円)。
        # 押さえで鉄板を削ると当たってもガミ(11/17に3本)。上乗せなら11/17の9Rで回収59%→69%
        # 2026-11-21 ご主人様「絞りなよ！」(みなもさんは2点で11月81.6%・直近5日92.2%、本予想は69.1%・67.3%):
        # 上位2点だけ・1レース400円(安い目に厚く)。押さえは全部やめる(11/17〜20で4,800円→1,080円・22.5%)。
        # 実績の買い目で試算すると 11/14〜20の56R: 上位6点65.7% → 上位2点112.9%(的中22本)。11/4〜の144R: 61.4% → 86.3%
        old_top2 = [c for c, _q in picks[:2]]
        # 2026-11-21夜 ご主人様「予想は任せるよ！」: 3連単2点を、みなもさんの形(頭1艇固定・2着3着の2艇を裏表)に変更。
        # 執事は2着を外しやすい(11/21: 2着外れ6R)。11/21の17Rで みなも型105.0% / 旧・上位2点(2着固定) 81.8%。
        # 旧の上位2点は v_top2 として並べて記録し、毎晩比べる
        _q1 = dict(top6)
        _ura = f"{a_}-{c_}-{b_}"
        picks = [(top6[0][0], top6[0][1]), (_ura, _q1.get(_ura, (pp or {}).get(_ura, 0.0)))]
        # 2026-11-26 ご主人様「みなもさんと同じ予想なら楽しく無いでしょ！執事君なりの予想出した方が良い」:
        # 11/21〜25 の本予想は 97R中78Rがみなもさんと同じ2点だった(一番来そうな1点とその裏 = 人気の目になりやすい)。
        # 執事の目: 頭は執事の1番手のまま、2着3着は「裏表2点の確率の合計」が一番高い2艇にする(1点の確率ではなく2点で考える)。
        # 2026年8〜10月の700R(頭1号艇・自信B以上)で試算: 今の決め方 83.9%(175本) → 2点合計で選ぶ 86.8%(179本)。
        # 今までの決め方(1番手とその裏)は v_mshape として並べて記録し、毎晩比べる
        try:
            _pa = pp or ai3.probs(rows, int(stadium_code), int(race_number), (weather or {}).get("wind"),
                                  (weather or {}).get("wave")) or {}
            if _pa:
                import itertools as _it
                _oth = [str(l) for l in range(1, 7) if str(l) != a_]
                _x, _y = max(_it.combinations(_oth, 2),
                             key=lambda t: _pa.get(f"{a_}-{t[0]}-{t[1]}", 0) + _pa.get(f"{a_}-{t[1]}-{t[0]}", 0))
                _c1, _c2 = sorted((f"{a_}-{_x}-{_y}", f"{a_}-{_y}-{_x}"), key=lambda c: -_pa.get(c, 0))
                picks = [(_c1, _pa.get(_c1, 0.0)), (_c2, _pa.get(_c2, 0.0))]
                if {_x, _y} != {b_, c_}:
                    print(f"執事の本予想 {stadium_name}{race_number}R: 執事の目 {_c1}/{_c2}(今までの決め方なら {top6[0][0]}/{_ura})")
        except Exception as e:
            print(f"執事の目の失敗(今までの決め方で出す): {e}")
        has_g, yon, man = True, None, None
        # 2026-11-21 ご主人様「任せる」: 1レース600円のまま、3連単200円(2点×100円)+3連複400円に配分を変更。
        # 執事の読みは着順より3艇の顔ぶれが確か(137R: 3連単2点 82.6% / 3連複1点 91.2%)。旧: 3連単400円+3連複200円 85.5% → 約88%
        alloc = ai3.allocate_even(picks, odds_s, total=200)
        if not has_g:
            ana2 = gyaku
            picks.append((gyaku, 0.0))
            alloc.append((gyaku, 100))
        if yon:
            picks.append((yon, 0.0))
            alloc.append((yon, 100))
        if man:
            picks.append((man, 0.0))
            alloc.append((man, 100))
        pmap = dict(picks)
        picks = [(c, pmap[c]) for c, _a in alloc]
        amt = [a for _c, a in alloc]
        tickets, lines = [], []
        for (combo, p), a in zip(picks, amt):
            o = (odds_map or {}).get(tuple(map(int, combo.split("-"))))
            tickets.append(BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                                     race_number=race_number, combination=combo, amount=a, bet_type="gen", odds=o))
            lines.append(f"{combo} {a}円" + (f"({o}倍)" if o else ""))
        save_bet_tickets(state, tickets)
        try:  # 旧・上位2点(2着固定)の記録(紙上・各100円)
            save_bet_tickets(state, [BetTicket(race_date=str(target_date), session=session_name,
                                               stadium_code=stadium_code, race_number=race_number, combination=c,
                                               amount=100, bet_type="v_top2",
                                               odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
                                     for c in old_top2])
        except Exception as e:
            print(f"旧上位2点の記録失敗: {e}")
        # 2026-11-26夜 ご主人様「的中出来なすぎる！」「良いよ！」: 3着の取りこぼし対策を紙上で並べて記録(各100円・2点)。
        # 11/22〜26の本予想96R: ぴったり28 / 3着だけ外れ31(その3着は4号艇13・6号艇5・2号艇5・5号艇4・3号艇4) / 2着外れ17 / 頭外れ20。
        #  v_out3: 5・6号艇が展示タイム2位以内で本予想の3艇に入っていない時だけ、頭-2着候補-その外枠(2点)
        #  v_3rd : 毎レース、本予想の2艇以外で「頭-2着候補-◯」の確率の合計が一番高い3着候補1艇(2点)
        #  本予想(裏表2点)にこの2点を足して4点にした時の回収率を毎晩比べ、良ければ本予想に入れる
        try:
            _h, _x, _y = picks[0][0].split("-")
            _odd = lambda c: (odds_map or {}).get(tuple(map(int, c.split("-"))))
            _ext = []
            _exm = {str(b.get("lane")): b.get("exhibition_time") for b in (before_info or [])
                    if b.get("exhibition_time") is not None}
            if len(_exm) == 6:
                _ord = sorted(_exm, key=lambda l: float(_exm[l]))[:2]
                _o = [l for l in ("5", "6") if l in _ord and l not in (_h, _x, _y)]
                if _o:
                    _ext.append(("v_out3", _o[0]))
            _pz = pp or ai3.probs(rows, int(stadium_code), int(race_number), (weather or {}).get("wind"),
                                  (weather or {}).get("wave")) or {}
            _rest = [str(l) for l in range(1, 7) if str(l) not in (_h, _x, _y)]
            if _pz and _rest:
                _z = max(_rest, key=lambda z: _pz.get(f"{_h}-{_x}-{z}", 0) + _pz.get(f"{_h}-{_y}-{z}", 0))
                _ext.append(("v_3rd", _z))
            for _bt, _z in _ext:
                save_bet_tickets(state, [BetTicket(race_date=str(target_date), session=session_name,
                                                   stadium_code=stadium_code, race_number=race_number, combination=c,
                                                   amount=100, bet_type=_bt, odds=_odd(c))
                                         for c in (f"{_h}-{_x}-{_z}", f"{_h}-{_y}-{_z}")])
                print(f"3着の押さえ {_bt}: {stadium_name}{race_number}R {_h}-{_x}-{_z} / {_h}-{_y}-{_z}")
        except Exception as e:
            print(f"3着の押さえの記録失敗: {e}")
        # 2026-11-21 ご主人様「みなもさんを勉強して頭脳を吸収して」: みなもさんの形(頭1艇固定・2着3着の2艇を裏表)を
        # 執事の1番手で作った買い目を、並べて記録(紙上・各100円)。本予想(上位2点)とどちらが良いか毎晩比べる。
        # 11/4〜の同じレースで、1点だけ一致したレースはみなもさんの残り1点(裏表)が 90.7% / 執事の2番手 66.9%。
        try:
            a1, b1, c1 = top6[0][0].split("-")
            save_bet_tickets(state, [BetTicket(race_date=str(target_date), session=session_name,
                                               stadium_code=stadium_code, race_number=race_number, combination=c,
                                               amount=100, bet_type="v_mshape",
                                               odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
                                     for c in (f"{a1}-{b1}-{c1}", f"{a1}-{c1}-{b1}")])
        except Exception as e:
            print(f"みなも型の記録失敗: {e}")
        # 2026-11-21 ご主人様「本予想出す時に3連単と3連複を出せば？」: 執事は3艇の顔ぶれは掴めるが順番を外しやすい
        # (11/4〜の本予想137R: 1番手の3艇で3連複1点なら 的中38.0%・回収91.2% / 3連単2点 28.5%・82.6%)。
        # 1番手の3艇の3連複1点(TRIO_AMT円)を本予想と一緒に出す(bet_type='gen_trio'、組は '1=2=3' の形)
        trio = None
        try:
            # 2026-11-26夜 ご主人様OK: 3連複は本予想の3連単(裏表)と同じ3艇にそろえる(住之江1Rで 1-2-3/1-3-2 なのに 1=2=4 だった)
            trio = "=".join(sorted(picks[0][0].split("-")))
            save_bet_tickets(state, [BetTicket(race_date=str(target_date), session=session_name,
                                               stadium_code=stadium_code, race_number=race_number, combination=trio,
                                               amount=TRIO_AMT, bet_type="gen_trio", odds=None)])
        except Exception as e:
            print(f"3連複の記録失敗: {e}")
            trio = None
        # 2026-11-08 ご主人様指示: 「AI」と呼ばない。執事が責任を持って出す予想として、根拠(レースの中身)も添える
        why = ai3.reasons(rows, [c for c, _p in top6], (weather or {}).get("wind"))
        lab = {"S": "自信S", "A": "自信A", "B": "自信B"}.get(tier, "本命レース") + ("・👩レディース" if ladies else "")
        lines = [("・逆転の穴 " if c == ana2 else "・4号艇2着の押さえ " if c == yon else "・万舟の押さえ " if c == man else "・穴 " if ana and c == ana[0] else "・鉄板 ") + x
                 for (c, _p), x in zip(picks, lines)]
        _save_tier(target_date, stadium_code, race_number, tier, top6[0][0], top6[0][1], main=True)
        msg = (f"🎩 **執事の本予想** {stadium_name}{race_number}R" + (f"(締切{deadline})" if deadline else "")
               + f"  {lab}\n【3連単】\n" + "\n".join(lines)
               + (f"\n【3連複】\n・{trio} {TRIO_AMT}円" if trio else "")
               + f"\n投資：{sum(amt) + (TRIO_AMT if trio else 0):,}円(3連単{sum(amt):,}円" + (f" + 3連複{TRIO_AMT}円" if trio else "") + ") ※紙上\n"
               + "【根拠】" + " / ".join(why))
        print(msg)
        try:
            notify_discord(msg)
            time.sleep(1.2)
        except Exception as e:
            print(f"執事の本予想の通知失敗: {e}")
        # 2026-11-23 ご主人様「コロガシは必ずやりなさい！1レースやって2レース目をやらないなんてお門違い」:
        # 1戦目は自信Sだけ。1戦目が当たったら、2戦目は次の本予想レース(自信を問わない)で必ず買う。
        # その日に本予想レースが残っていなければ、翌日の最初の本予想レースへ持ち越す(korogashi.load)
        _run_koro(state, target_date, session_name, stadium_code, race_number, picks, odds_s, stadium_name,
                  ana[0] if ana else None, deadline, is_s=(tier == "S"))
        # 2026-11-25 ご主人様「10000円を3回コロガシ」「やってみるか！」: 夢コロガシ(10,000円・3連チャン目標)も同じレースで
        from src.tools.korogashi import YUME
        _run_koro(state, target_date, session_name, stadium_code, race_number, picks, odds_s, stadium_name,
                  ana[0] if ana else None, deadline, is_s=(tier == "S"), cfg=YUME)
    except Exception as e:
        print(f"執事の本予想の失敗 {race_number}R: {e}")


# 2026-11-27夜 ご主人様「良いよ！」: 自信S/A/Bがどこにも残らず、自信別の的中率が測れなかった(朝・昼のログにも出ない)。
# 執事の本予想で読んだレースの自信を plans/tiers/日付.csv に残す(main=1 は本予想として出したレース)。
def _save_tier(target_date, stadium_code, race_number, tier, top, prob, main=False) -> None:
    try:
        import csv as _csv
        d = os.path.join("plans", "tiers")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{target_date}.csv")
        cols = ["stadium_code", "race_number", "tier", "top", "top_prob", "main", "updated"]
        rows = []
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                rows = list(_csv.DictReader(f))
        key = (str(stadium_code).zfill(2), str(int(race_number)))
        old = next((r for r in rows if (r["stadium_code"], r["race_number"]) == key), None)
        new = {"stadium_code": key[0], "race_number": key[1], "tier": tier or "", "top": top,
               "top_prob": f"{prob:.4f}", "main": "1" if (main or (old and old.get("main") == "1")) else "0",
               "updated": datetime.now(JST).strftime("%H:%M:%S")}
        rows = [r for r in rows if r is not old] + [new]
        rows.sort(key=lambda r: (r["stadium_code"], int(r["race_number"])))
        with open(p, "w", encoding="utf-8", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    except Exception as e:
        print(f"自信の記録失敗 {race_number}R: {e}")


# 2026-11-10 ご主人様「Sレースで2連チャンの練習してみるか」: コロガシ練習(紙上・集計外)。中身は src/tools/korogashi.py
KORO_RULES = [("v_koro", "コロガシ練習(10,000円から・1戦目は自信S・2戦目は次の本予想で必ず・集計外)"),
              ("v_koro3", "夢コロガシ(10,000円から・3連チャン目標・1〜3戦目すべて自信Sだけ・集計外)")]


def _run_koro(state, target_date, session_name, stadium_code, race_number, picks, odds_s, stadium_name, ana,
              deadline=None, is_s=True, cfg=None) -> None:
    try:
        from src.models import ai3
        from src.tools import korogashi as K
        cfg = cfg or K.KORO
        lab, goal = cfg["label"], cfg["goal"]
        st = K.load(target_date, cfg)
        if not is_s and not st.get("pending") and int(st.get("chain", 0)) == 0:
            return  # 1戦目は自信Sだけ
        if not is_s and cfg.get("s_only"):
            # 2026-11-27夜 ご主人様「3連ちゃんはS評価のみでやるように！」: 2戦目・3戦目も自信Sだけ。
            # 自信S以外のレースでは買わない(前のレースの精算だけして、払戻は次の自信Sへ持ち越す)
            ok, rep = K.resolve(target_date, st, cfg)
            if rep:
                print(rep)
                notify_discord(rep)
                time.sleep(1.2)
            K.save(target_date, st, cfg)
            return
        ok, rep = K.resolve(target_date, st, cfg)
        if rep:
            print(rep)
            notify_discord(rep)
            time.sleep(1.2)
        if not ok:
            K.save(target_date, st, cfg)
            print(f"{lab}: 前のレースの結果待ちなので {stadium_name}{race_number}R は見送り(次の本予想レースで)")
            return
        if not is_s and int(st.get("chain", 0)) == 0:
            K.save(target_date, st, cfg)  # 1戦目が外れた後は、次の自信Sまで待つ
            return
        bank = int(st["bank"])
        # 2026-11-15 本予想と同じく、安い目に厚く・高い目は薄く
        alloc = ai3.allocate_even(picks, odds_s, total=bank // 100 * 100)
        tickets, lines = [], []
        for c, a in alloc:
            o = odds_s.get(c)
            tickets.append(BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                                     race_number=race_number, combination=c, amount=a, bet_type=cfg["bt"], odds=o))
            lines.append(f"・{c} {a:,}円" + (f"({o}倍)" if o else ""))
        save_bet_tickets(state, tickets)
        stake = sum(a for _c, a in alloc)
        st["pending"] = {"stadium_code": stadium_code, "race_number": int(race_number), "name": stadium_name,
                         "alloc": alloc, "stake": stake, "chain_races": st.get("chain_races", []),
                         "date": str(target_date)}
        K.save(target_date, st, cfg)
        n = st["chain"] + 1
        msg = (f"🎲 **{lab}({n}戦目)** {stadium_name}{race_number}R" + (f"(締切{deadline})" if deadline else "")
               + f"  元手{stake:,}円\n" + "\n".join(lines)
               + (f"\n※{n}戦目。当たれば{goal}連チャン成功！" if n >= goal
                  else f"\n※{n}戦目。当たったら払戻を全部、次の" + ("自信Sのレース" if cfg.get("s_only") else "本予想レース") + f"へ({goal}連チャンが目標)"))
        print(msg)
        notify_discord(msg)
        time.sleep(1.2)
    except Exception as e:
        print(f"{(cfg or {}).get('label', 'コロガシ')}の失敗 {race_number}R: {e}")


# 2026-11-10 ご主人様「風が強い日は万舟の法則」→ 同日「頭の5・6は良いがレースが違う」を受けて作り直し。
# 荒天万舟枠(紙上・お楽しみ): 風5m以上 × 執事の荒れ度(1号艇が負ける確率)60%以上 →
#   執事の読みで「1号艇以外が頭」の上位8点を100円ずつ。
# 2026年の風5m以上(答えを見ていない1,628R・1日約5.6R)で 的中28.9%・回収79%
# (前のルール「外A級頭-1-全」は的中6.9%。11/10は宮島・福岡で1号艇が2着にも残らず全滅)
ARE_RULES = [("v_are", "荒天万舟枠((風5m以上 or その日の荒れ水面)×荒れ度60%以上・本命水面は見送り・100倍以上8点)"),
             ("v_are_p", "荒天万舟 練習(11/10だけ・集計外)")]


def _run_are(state, target_date, session_name, stadium_code, race_number, odds_map, rows, weather, stadium_name="",
             deadline=None) -> None:
    # 2026-11-17 ご主人様OK: 万舟枠(荒天万舟・万舟狙い)はいったん止める(16〜17日23R 0本。来にくい目の順番は当てにならない)。
    # 万舟の取り方が見つかったら作り直す。万舟は本予想の押さえ(逆転・4号艇2着・1-5/1-6)で拾う
    if MANSHU_SLOTS_STOPPED:
        return
    try:
        from src.models import ai3
        wind = (weather or {}).get("wind")
        # 2026-11-10 ご主人様「今日の万舟は練習。データに入れない。思い切って仕掛けてみろ」:
        # 11/10だけ 風4m以上・荒れ度50%以上に広げ、上位8点に「100倍以上で一番来そうな2点」を足す。記録は v_are_p(集計外)
        practice = str(target_date) == "2026-11-10"
        w_min, a_min = (4, 0.5) if practice else (5, 0.6)
        # 2026-11-16 ご主人様「江戸川全然荒れてない。徳山の方が荒れてる」: 風だけで決めず、その日の水面を見る。
        #  ・その日その場で1号艇が4R以上走って6割以上負けている「荒れ水面」なら、風が弱くても狙う
        #  ・3R以上走って1号艇が7割近く勝っている「本命水面」なら、風が強くても見送る
        n_prev, lose = 0, None
        try:
            from src.tools.day_trend import boat1_lose_rate
            n_prev, lose = boat1_lose_rate(stadium_code, int(race_number), target_date)
        except Exception as e:
            print(f"その日の流れの計算失敗: {e}")
        rough_day = n_prev >= 4 and lose is not None and lose >= 0.6
        # 2026-11-17 ご主人様「みなもさんの予想を加味しなさい」: みなもさんの◎が1号艇以外のレースは万舟33%。
        # 風や水面に関係なく狙い、頭はみなもさんの◎の艇を優先する
        mk = minamo_mark(target_date, stadium_code, race_number)
        m_head = mk[1] if mk and len(mk) > 1 and mk[1] != "1" else None
        calm_day = n_prev >= 3 and lose is not None and lose <= 0.34
        windy = wind is not None and float(wind) >= w_min
        if calm_day and not m_head:
            if windy:
                print(f"荒天万舟枠 {stadium_name}{race_number}R: 風{wind}mでも今日この場は1号艇{n_prev}R中"
                      f"{n_prev - round(lose * n_prev)}勝の本命水面なので見送り")
            return
        if not (windy or rough_day or m_head):
            return
        are = ai3.boat1_lose(rows, int(stadium_code), int(race_number), wind, (weather or {}).get("wave"))
        trend = ""
        if are is not None and n_prev >= 4 and lose is not None:
            trend = f"・今日この場の1号艇は{n_prev}R中{round(lose * n_prev)}回負け"
            if lose >= 0.75:  # 実際の1号艇は読みより約5pt弱い
                are += 0.05
        if are is None or (are < a_min and not m_head):
            return
        p = ai3.probs(rows, int(stadium_code), int(race_number), wind, (weather or {}).get("wave")) or {}
        ranked = [c for c, _q in sorted(p.items(), key=lambda x: -x[1]) if not c.startswith("1-")]
        # 2026-11-15 ご主人様「万舟狙いで3-1-6の5.5倍は要らない。皆100倍無い。これじゃ一生万舟出ない」:
        # 万舟(100円→10,000円以上)になる100倍以上の目だけから、来やすい順に8点。オッズが取れない時は買わない
        oo = {"-".join(map(str, k)): float(v) for k, v in (odds_map or {}).items() if v}
        combos = [c for c in ranked if oo.get(c, 0) >= 100]
        if m_head:  # みなもさんの◎の艇が頭の目を先に
            combos = [c for c in combos if c[0] == m_head][:5] + [c for c in combos if c[0] != m_head]
            trend += f"・みなもさんの◎は{m_head}号艇"
        combos = combos[:8]
        if not combos:
            print(f"荒天万舟枠 {stadium_name}{race_number}R: 100倍以上の目が取れず見送り")
            return
        tickets, lines = [], []
        for c in combos:
            o = (odds_map or {}).get(tuple(map(int, c.split("-"))))
            tickets.append(BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                                     race_number=race_number, combination=c, amount=100,
                                     bet_type="v_are_p" if practice else "v_are", odds=o))
            lines.append(f"・万舟 {c} 100円" + (f"({o}倍)" if o else ""))
        save_bet_tickets(state, tickets)
        heads = sorted({c[0] for c in combos}, key=lambda h: -sum(p[c] for c in combos if c[0] == h))
        why = ai3.reasons(rows, combos, wind)
        msg = (f"🌪️ **荒天万舟枠{'(練習・集計外)' if practice else ''}・100倍以上の目だけ** {stadium_name}{race_number}R" + (f"(締切{deadline})" if deadline else "")
               + f"  風{wind}m・荒れ度{are * 100:.0f}%{trend}\n" + "\n".join(lines)
               + f"\n投資：{len(combos) * 100:,}円／{len(combos)}点 ※紙上・お楽しみ\n"
               + f"【根拠】頭の本線は{'・'.join(h + '号艇' for h in heads[:2])} / " + " / ".join(why))
        print(msg)
        notify_discord(msg)
        time.sleep(1.2)
    except Exception as e:
        print(f"荒天万舟枠の失敗 {race_number}R: {e}")


# 2026-11-13 ご主人様「この情報(予想家の万舟狙いレース)を元に執事も万舟を出してみろ」: 注目レースの万舟狙い(紙上・練習)。
# tipster/watch_日付.csv に 場・R・頭にする艇 を書いておくと、そのレースの締切前に、
# 「その艇が頭」の組から執事の読みで上位8点を100円ずつ買う。記録は v_watch。
# 2026-11-16 ご主人様OK「みなもさんの当たる会場と当たらない会場を精査すれば楽に回収率稼げる」:
# 「みなも×強い場」枠(紙上)。毎朝、その日より前のみなもさんの先出し(tipster/minamo_all_*.csv)を場ごとに採点し、
# 24R以上で回収率110%以上の場だけ、みなもさんの2点(◎-○-▲ / ◎-▲-○)を200円ずつ買う。記録は v_minamo。
# 場の選び方はその朝までの成績だけで決める(後から良かった場を選ぶズルにしない)。
def minamo_mark(target_date, stadium_code, race_number) -> str | None:
    """みなもさんの先出しの印("◎1○3▲2")。無ければ None"""
    import csv as _csv
    path = os.path.join("tipster", f"minamo_all_{target_date}.csv")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            if r["stadium_code"].zfill(2) == str(stadium_code).zfill(2) and int(r["race_number"]) == int(race_number):
                return r.get("marks") or None
    return None


# 2026-11-21 ご主人様「色々な予想師を色々な角度で検証して高精度な予想を」「各予想師の的中する会場を分析して」→「良いよ」:
# 予想家会議(紙上・1週間お試し)。執事・みなもさん・公式PCの「1番目の予想」を集め、3艇の顔ぶれが全員一致した時だけ買う。
# みなもさんが得意な会場(minamo_strong_venues: その日までの成績で回収110%以上)では、みなもさんの意見を2票にする。
# 11/4〜18の検証: 3艇全員一致 562R 的中29.4%・回収78.3%(本予想の形 3連単2点+3連複400円)。
# 会場の得意を毎日入れ替える形は 66〜68% で効かなかった(しねん・あそかは得意会場が入れ替わる)ので、今は票の重みだけに使う。
KAIGI_RULES = [("v_kaigi", "予想家会議(執事・みなも・公式PCの3艇が全員一致) 3連単2点各100円+3連複400円")]


def _pc_top(target_date, stadium_code, race_number):
    import csv as _csv
    path = os.path.join("tipster", f"pc_{target_date}.csv")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            if r["stadium_code"].zfill(2) == str(stadium_code).zfill(2) and int(r["race_number"]) == int(race_number):
                cs = (r.get("combos") or "").split()
                return cs[0] if cs else None
    return None


def _minamo_top(target_date, stadium_code, race_number):
    import csv as _csv
    path = os.path.join("tipster", f"minamo_all_{target_date}.csv")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            if r["stadium_code"].zfill(2) == str(stadium_code).zfill(2) and int(r["race_number"]) == int(race_number):
                cs = (r.get("combos") or "").split()
                return cs[0] if cs else None
    return None


def _run_kaigi(state, target_date, session_name, stadium_code, race_number, odds_map, top6, stadium_name="") -> None:
    try:
        ops = [("執事", top6[0][0], 1)]
        mi = _minamo_top(target_date, stadium_code, race_number)
        if mi:
            strong = str(stadium_code).zfill(2) in minamo_strong_venues(target_date)
            ops.append(("みなも", mi, 2 if strong else 1))
        pc = _pc_top(target_date, stadium_code, race_number)
        if pc:
            ops.append(("公式PC", pc, 1))
        if len(ops) < 3:
            return
        tot = sum(w for _n, _c, w in ops)
        sets, combos = {}, {}
        for _n, c, w in ops:
            k = "=".join(sorted(c.split("-")))
            sets[k] = sets.get(k, 0) + w
            combos[c] = combos.get(c, 0) + w
        trio, votes = max(sets.items(), key=lambda x: x[1])
        if votes < tot:
            return
        buy3 = [c for c, _v in sorted(combos.items(), key=lambda x: -x[1])][:2]
        if len(buy3) < 2:  # 全員が同じ3連単なら、執事の2番手で2点目
            buy3 += [c for c, _p in top6 if c not in buy3][:1]
        tickets = [BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                             race_number=race_number, combination=c, amount=100, bet_type="v_kaigi",
                             odds=(odds_map or {}).get(tuple(map(int, c.split("-"))))) for c in buy3]
        tickets.append(BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                                 race_number=race_number, combination=trio, amount=400, bet_type="v_kaigi", odds=None))
        save_bet_tickets(state, tickets)
        print(f"予想家会議 {stadium_name}{race_number}R: " + " / ".join(f"{n}{c}" + ("(2票)" if w == 2 else "") for n, c, w in ops)
              + f" → 3連単 {' '.join(buy3)} + 3連複 {trio}")
    except Exception as e:
        print(f"予想家会議の失敗 {race_number}R: {e}")


MINAMO_RULES = [("v_minamo", "みなも×強い場(先出し2点を200円ずつ・その朝までの成績で場を選ぶ)"),
                ("v_mi_not1", "みなも◎が1号艇以外(先出し2点を100円ずつ・本予想が見送るレース)")]


# 2026-11-25夜 ご主人様「良いよ！」: 本予想は「みなもさんの◎が1号艇以外なら見送り」だが、そのレースのみなもさんは
# 11/21〜25の5日で93R・回収113.8%(◎1号艇の696Rは75.4%)。見送っているレースを紙上で記録して確かめる。
# みなもさんの先出し2点(◎-○-▲ / ◎-▲-○)をそのまま100円ずつ。記録は v_mi_not1。
def _run_mi_not1(state, target_date, session_name, stadium_code, race_number, odds_map) -> None:
    try:
        import csv as _csv
        path = os.path.join("tipster", f"minamo_all_{target_date}.csv")
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            row = next((r for r in _csv.DictReader(f) if r["stadium_code"].zfill(2) == str(stadium_code).zfill(2)
                        and int(r["race_number"]) == int(race_number)), None)
        if not row:
            return
        combos = row["combos"].split()
        if not combos or combos[0][0] == "1":
            return
        save_bet_tickets(state, [BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                                           race_number=race_number, combination=c, amount=100, bet_type="v_mi_not1",
                                           odds=(odds_map or {}).get(tuple(map(int, c.split("-")))))
                                 for c in combos])
        print(f"みなも◎1号艇以外 v_mi_not1: {stadium_code} {race_number}R {row.get('marks', '')} " + " ".join(combos))
    except Exception as e:
        print(f"みなも◎1号艇以外の失敗 {race_number}R: {e}")
MINAMO_MIN_RACES, MINAMO_MIN_RET = 24, 1.10


def minamo_strong_venues(target_date) -> dict:
    """{場コード: (レース数, 的中, 回収率)} その日より前の成績で、24R以上・回収率110%以上の場。plans/minamo_venues に記録"""
    import csv as _csv
    import glob
    cache = os.path.join("plans", "minamo_venues", f"{target_date}.json")
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return {k: tuple(v) for k, v in json.load(f).items()}
    acc = {}
    for fp in sorted(glob.glob(os.path.join("tipster", "minamo_all_*.csv"))):
        d = os.path.basename(fp)[len("minamo_all_"):-4]
        if d >= str(target_date):
            continue
        rp = os.path.join("plans", "day_results", f"{d}.csv")
        if not os.path.exists(rp):
            continue
        with open(rp, encoding="utf-8") as f:
            res = {(r["stadium_code"].zfill(2), int(r["race_number"])): (r["trifecta"], int(r.get("payout") or 0))
                   for r in _csv.DictReader(f)}
        with open(fp, encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                k = (r["stadium_code"].zfill(2), int(r["race_number"]))
                if k not in res:
                    continue
                tri, pay = res[k]
                v = acc.setdefault(k[0], [0, 0, 0])
                v[0] += 1
                if tri in r["combos"].split():
                    v[1] += 1
                    v[2] += pay
    out = {s: (v[0], v[1], round(v[2] / (v[0] * 200), 3)) for s, v in acc.items()
           if v[0] >= MINAMO_MIN_RACES and v[2] / (v[0] * 200) >= MINAMO_MIN_RET}
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


def _run_minamo(state, target_date, session_name, stadium_code, race_number, odds_map, stadium_name="",
                deadline=None) -> None:
    try:
        import csv as _csv
        strong = minamo_strong_venues(target_date)
        if stadium_code not in strong:
            return
        path = os.path.join("tipster", f"minamo_all_{target_date}.csv")
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            row = next((r for r in _csv.DictReader(f) if r["stadium_code"].zfill(2) == stadium_code
                        and int(r["race_number"]) == int(race_number)), None)
        if not row:
            return
        combos = row["combos"].split()
        tickets, lines = [], []
        for c in combos:
            o = (odds_map or {}).get(tuple(map(int, c.split("-"))))
            tickets.append(BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                                     race_number=race_number, combination=c, amount=200, bet_type="v_minamo", odds=o))
            lines.append(f"・{c} 200円" + (f"({o}倍)" if o else ""))
        save_bet_tickets(state, tickets)
        n, h, ret = strong[stadium_code]
        msg = (f"🌊 **みなも×強い場(紙上)** {stadium_name}{race_number}R" + (f"(締切{deadline})" if deadline else "")
               + f"  {row.get('marks', '')}\n" + "\n".join(lines)
               + f"\n投資：{len(combos) * 200:,}円／{len(combos)}点 ※紙上\n"
               + f"【根拠】みなもさんのこの場の成績 {n}R中{h}的中・回収率{ret * 100:.0f}%(昨日まで)")
        print(msg)
        notify_discord(msg)
        time.sleep(1.2)
    except Exception as e:
        print(f"みなも×強い場の失敗 {race_number}R: {e}")


MANSHU_SLOTS_STOPPED = True
WATCH_RULES = [("v_watch", "注目レースの万舟狙い(予想家の着眼×執事の読み・練習)")]


def _run_watch(state, target_date, session_name, stadium_code, race_number, odds_map, rows, weather,
               stadium_name="", deadline=None) -> None:
    # 2026-11-17 ご主人様OK: 万舟枠(荒天万舟・万舟狙い)はいったん止める(16〜17日23R 0本。来にくい目の順番は当てにならない)。
    # 万舟の取り方が見つかったら作り直す。万舟は本予想の押さえ(逆転・4号艇2着・1-5/1-6)で拾う
    if MANSHU_SLOTS_STOPPED:
        return
    try:
        import csv as _csv
        path = os.path.join("tipster", f"watch_{target_date}.csv")
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            w = [r for r in _csv.DictReader(f)
                 if r["stadium_code"].zfill(2) == stadium_code and int(r["race_number"]) == int(race_number)]
        if not w:
            return
        heads = w[0]["heads"].split()
        from src.models import ai3
        p = ai3.probs(rows, int(stadium_code), int(race_number), (weather or {}).get("wind"),
                      (weather or {}).get("wave")) or {}
        # 2026-11-14 ご主人様「万舟は100円が10,000円を超えて初めて万舟」: 万舟狙いは100倍以上の目だけから選ぶ
        # (オッズが取れない目は確率の順で残す)
        def _o(c):
            return (odds_map or {}).get(tuple(map(int, c.split("-"))))
        combos = [c for c, _q in sorted(p.items(), key=lambda x: -x[1])
                  if c[0] in heads and _o(c) is not None and float(_o(c)) >= 100][:8]
        if not combos:
            return
        tickets, lines = [], []
        for c in combos:
            o = (odds_map or {}).get(tuple(map(int, c.split("-"))))
            tickets.append(BetTicket(race_date=str(target_date), session=session_name, stadium_code=stadium_code,
                                     race_number=race_number, combination=c, amount=100, bet_type="v_watch", odds=o))
            lines.append(f"・万舟 {c} 100円" + (f"({o}倍)" if o else ""))
        save_bet_tickets(state, tickets)
        why = ai3.reasons(rows, combos, (weather or {}).get("wind"))
        msg = (f"🎯 **執事の万舟狙い(練習・100倍以上の目だけ)** {stadium_name}{race_number}R" + (f"(締切{deadline})" if deadline else "")
               + f"\n着眼：{w[0].get('note', '')}\n" + "\n".join(lines)
               + f"\n投資：{len(combos) * 100:,}円／{len(combos)}点 ※紙上\n【根拠】" + " / ".join(why))
        print(msg)
        notify_discord(msg)
        time.sleep(1.2)
    except Exception as e:
        print(f"注目レースの万舟狙いの失敗 {race_number}R: {e}")


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
        if CANCELLED.get((stadium_code, str(target_date))):
            print(f"{label}: 開催中止 → 対象外")
            _record(state, target_date, session_name, stadium_code, race_number, "cancelled")
            continue
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
            # 2026-11-19 執事: 津3R・4Rが強風で中止になり、オッズ側の RaceCanceled が「最終予想失敗+Traceback」として出ていた。
            # 公式の中止は不具合ではないので、開催中止として静かに外す(本物の失敗と見分けるため)
            if type(e).__name__ == "RaceCanceled":
                print(f"{label}: レース中止(公式) → 対象外")
                _record(state, target_date, session_name, stadium_code, race_number, "cancelled")
                continue
            # 2026-11-18 執事: 若松8R・9Rが「最終予想失敗 」(理由が空)で落ちた。原因が見えるよう例外の型と場所を必ず出す
            import traceback as _tb
            print(f"{label}: 最終予想失敗 {type(e).__name__}: {e!r}")
            print(_tb.format_exc()[-1500:])
            # 締切が迫っていればあきらめる。まだ時間があれば次回再挑戦。
            if mins is not None and mins < 10:
                _record(state, target_date, session_name, stadium_code, race_number, "missed")
            continue

        is_main = race.get("main", True)
        if not (race.get("verify") and not race.get("gen") and not is_main):
            _run_experiment(state, target_date, session_name, stadium_code, race_number, label, indexed, odds_map)
        # ===== 本予想(2026-10-14〜): 人気上位5点に票が集中しているレースだけ =====
        # 対象は今処理している全レース(厳選候補・江戸川津・検証枠)。モデルの鉄板5点は使わない。
        done_n = _main_count(state, target_date) if str(target_date) >= "2026-11-05" else _main_count_session(state, target_date, session_name)
        need = MAIN_TARGET_PER_SESSION - done_n
        remaining = sum(1 for sn, r in todo if sn == session_name
                        and (r.get("deadline") or "99:99") >= (race.get("deadline") or "99:99"))
        thr = _quota_threshold(need, remaining)
        if str(target_date) >= "2026-11-05":
            thr = MAIN_MIN_CONC_NEW
        pop_plans, conc = build_popular_plan(odds_map, min_concentration=min(thr, 1.0))
        print(f"{label}: 集中度{conc * 100:.1f}% (本予想{done_n}/{MAIN_TARGET_PER_SESSION}R・残り{remaining}R・下限{thr * 100:.0f}%)")
        skip = _main_skip_reason(card, before_info)
        if skip and remaining <= need * 1.5:
            skip = None  # 10Rに届かなくなりそうな時は足切りしない
        if pop_plans and skip:
            print(f"{label}: 本予想は見送り({skip})")
            pop_plans = []
        if str(target_date) >= "2026-11-05" and pop_plans:
            if stadium_code in MAIN_BAD_STADIUMS:
                print(f"{label}: 本予想は見送り(三国・鳴門は除外)")
                pop_plans = []
            else:
                pop_plans = _filter_main_by_value(pop_plans, _adj_values(
                    odds_map, card, before_info, LAST_WEATHER.get((stadium_code, race_number, str(target_date)), {})), label)
        if str(target_date) < "2026-11-05" and pop_plans and done_n >= MAIN_MAX_PER_SESSION:
            print(f"{label}: 本予想は{session_ja}で既に{MAIN_MAX_PER_SESSION}R買ったので見送り")
            pop_plans = []
        # 2026-11-06 ご主人様決定: 本予想(3連単)は廃止。7.6万Rの検証で、どう絞っても回収率80%前後が限界だったため。
        # メインはリーサルウェポン(1号艇単勝)、お楽しみは新456枠2つ。
        if str(target_date) >= MAIN_STOP_DATE:
            pop_plans = []
        if str(target_date) >= "2026-11-05" and pop_plans and _main_count(state, target_date) >= MAIN_MAX_PER_DAY:
            print(f"{label}: 本予想は今日すでに{MAIN_MAX_PER_DAY}R買ったので見送り")
            pop_plans = []
        # 2026-11-08 ご主人様指摘「大穴万舟を入れたら難しい」: 10/1〜の万舟(100倍以上)の点は230点で的中0・回収0%。外す
        if pop_plans:
            pop_plans = [p for p in pop_plans if not (p.odds and p.odds >= 100)]
        # 2026-11-10 ご主人様「S・Aで10R」: 人気が集中する本命レースでも、執事の自信がS・Aでなければ買わない
        main_race = False
        b1_class = next((r.get("racer_class") for r in card.get("racers", []) if r.get("lane") == 1), None)
        weather = LAST_WEATHER.get((stadium_code, race_number, str(target_date)), {})
        # 全レースで1号艇の級と天候・風・波を記録(今後の条件づくりの材料)
        _save_meta(state, target_date, stadium_code, race_number, b1_class, weather)
        # 2026-11-06 ご主人様指示: 旧456系(江戸川全R 1-456-456・456BOX)は停止。新システムの2枠だけで検証
        _run_verify(state, target_date, session_name, stadium_code, race_number, b1_class, odds_map)
        _run_wind_verify(state, target_date, session_name, stadium_code, race_number, odds_map, weather)
        # 万舟検証枠(v_man_a/b)は 2026-11-06 ご主人様指示で廃止(10/20〜 780R 回収率10%)
        _run_outer_verify(state, target_date, session_name, stadium_code, race_number, odds_map, card, before_info)
        _run_exp2(state, target_date, session_name, stadium_code, race_number, odds_map, card, before_info, weather)
        _run_tanfuku(target_date, session_name, stadium_code, race_number, before_info, card, race.get("deadline"))
        _run_likely(state, target_date, session_name, stadium_code, race_number, odds_map)
        _run_456box(state, target_date, session_name, stadium_code, race_number, odds_map, card)
        _run_sel(state, target_date, session_name, stadium_code, race_number, odds_map, card, before_info)
        _run_mi_not1(state, target_date, session_name, stadium_code, race_number, odds_map)
        _run_1456b(state, target_date, session_name, stadium_code, race_number, odds_map, card)
        _run_edo15(state, target_date, session_name, stadium_code, race_number, odds_map, before_info)
        _run_venue(state, target_date, session_name, stadium_code, race_number, odds_map, card, weather)
        _run_ai3(state, target_date, session_name, stadium_code, race_number, odds_map, card, before_info, weather,
                 stadium_name, force=main_race, deadline=race.get("deadline"))

        # ===== 本番(2026-10-12 再構築): 会場×1号艇の級の条件ルール =====
        rule_plans = _rule_plans(stadium_code, race_number, b1_class, odds_map, weather) if is_main else None
        if str(target_date) >= MAIN_STOP_DATE:
            rule_plans = None  # 江戸川鉄板などの3連単本番も廃止
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
