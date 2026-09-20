"""
ボートレース公式サイトからデータを取得するモジュール
"""
import re
from datetime import date
from typing import Optional

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.boatrace.jp"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def fetch_today_stadiums(target_date: Optional[date] = None) -> list[dict]:
    """
    指定日(省略時は今日)に開催中のボートレース場一覧を取得する。

    Returns:
        [{"stadium_code": "01", "stadium_name": "桐生"}, ...]
    """
    if target_date is None:
        target_date = date.today()
    hd = target_date.strftime("%Y%m%d")

    url = f"{BASE_URL}/owpc/pc/race/index?hd={hd}"
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    res.encoding = res.apparent_encoding

    soup = BeautifulSoup(res.text, "html.parser")
    stadiums = []

    # 開催場一覧のテーブル行をパース
    for link in soup.select("a[href*='raceindex'][href*='jcd=']"):
        href = link.get("href", "")
        m = re.search(r"jcd=(\d+)", href)
        if not m:
            continue
        stadium_code = m.group(1)
        stadium_name = link.get_text(strip=True)
        if not stadium_name:
            continue

        stadiums.append({
            "stadium_code": stadium_code,
            "stadium_name": stadium_name,
        })

    # 重複除去(同じ場が複数リンクに出ることがあるため)
    seen = set()
    unique_stadiums = []
    for s in stadiums:
        if s["stadium_code"] not in seen:
            seen.add(s["stadium_code"])
            unique_stadiums.append(s)

    return unique_stadiums


def fetch_race_card(stadium_code: str, race_number: int, target_date: Optional[date] = None) -> dict:
    """
    指定場・指定レースの出走表を取得する。

    Args:
        stadium_code: 場コード(例 "01" = 桐生)
        race_number: レース番号(1〜12)
        target_date: 対象日(省略時は今日)

    Returns:
        {
            "stadium_code": "01",
            "race_number": 1,
            "date": "20260919",
            "racers": [
                {"lane": 1, "name": "選手名", "number": "1234", ...},
                ...
            ]
        }
    """
    if target_date is None:
        target_date = date.today()
    hd = target_date.strftime("%Y%m%d")

    url = (
        f"{BASE_URL}/owpc/pc/race/racelist"
        f"?rno={race_number}&jcd={stadium_code}&hd={hd}"
    )
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    res.encoding = res.apparent_encoding

    soup = BeautifulSoup(res.text, "html.parser")

    racers = []

    # 出走表テーブルの各艇(1〜6号艇)をパース
    for row in soup.select("tbody.is-fs12 tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        boat_color_cell = next(
            (c for c in cells if any(
                cls.startswith("is-boatColor") for cls in c.get("class", [])
            )),
            None
        )
        if boat_color_cell is None:
            continue

        try:
            lane = int(boat_color_cell.get_text(strip=True))
        except ValueError:
            continue

        name_tag = cells[1].find("a")
        name = name_tag.get_text(strip=True) if name_tag else None

        number_tag = cells[1].find("div", class_="is-fs11")
        racer_number = None
        if number_tag:
            m = re.search(r"\d{4}", number_tag.get_text())
            if m:
                racer_number = m.group()

        lineh2 = row.find_all("td", class_="is-lineH2")
        national_win_rate = None
        local_win_rate = None
        motor_2rate = None
        boat_2rate = None
        if len(lineh2) >= 5:
            try:
                national_win_rate = float(lineh2[1].get_text(separator="|").split("|")[0])
            except (ValueError, IndexError):
                pass
            try:
                local_win_rate = float(lineh2[2].get_text(separator="|").split("|")[0])
            except (ValueError, IndexError):
                pass
            try:
                motor_2rate = float(lineh2[3].get_text(separator="|").split("|")[1])
            except (ValueError, IndexError):
                pass
            try:
                boat_2rate = float(lineh2[4].get_text(separator="|").split("|")[1])
            except (ValueError, IndexError):
                pass

        racers.append({
            "lane": lane,
            "name": name,
            "number": racer_number,
            "national_win_rate": national_win_rate,
            "local_win_rate": local_win_rate,
            "motor_2rate": motor_2rate,
            "boat_2rate": boat_2rate,
        })

    return {
        "stadium_code": stadium_code,
        "race_number": race_number,
        "date": hd,
        "racers": racers,
    }


def fetch_race_deadline_times(stadium_code: str, target_date: Optional[date] = None) -> dict[int, str]:
    """
    指定場のその日の締切予定時刻(1R〜12R)を取得する。
    出走表ページ上部の「締切予定時刻」の行から、レース番号ごとの
    時刻(HH:MM)をまとめて取ってくる。1回のリクエストで場全体の
    12レース分が載っているので、レースごとに呼ぶ必要はない。

    Returns:
        {1: "10:10", 2: "10:41", ...}
    """
    if target_date is None:
        target_date = date.today()
    hd = target_date.strftime("%Y%m%d")

    url = f"{BASE_URL}/owpc/pc/race/racelist?rno=1&jcd={stadium_code}&hd={hd}"
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    res.encoding = res.apparent_encoding

    idx = res.text.find("締切予定時刻")
    if idx == -1:
        print("締切予定時刻の行が見つかりませんでした")
        return {}

    chunk = res.text[idx: idx + 4000]
    times = re.findall(r"\d{1,2}:\d{2}", chunk)
    print(f"締切予定時刻取得件数: {len(times)}件（本来12件）")

    return {race_number: t for race_number, t in enumerate(times[:12], start=1)}


def fetch_odds_3rentan(stadium_code: str, race_number: int, target_date: Optional[date] = None) -> dict[tuple[int, int, int], float]:
    """
    指定場・指定レースの3連単オッズを取得する。

    NOTE: このオッズページのHTML構造は未検証。取得件数が0件や
    120件から大きくズレる場合は、印字されるログをそのまま貼ってもらえれば調整する。

    Returns:
        {(1, 6, 5): 7.4, (1, 5, 6): 8.7, ...}
    """
    if target_date is None:
        target_date = date.today()
    hd = target_date.strftime("%Y%m%d")

    url = (
        f"{BASE_URL}/owpc/pc/race/odds3t"
        f"?rno={race_number}&jcd={stadium_code}&hd={hd}"
    )
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    res.encoding = res.apparent_encoding

    soup = BeautifulSoup(res.text, "html.parser")
    odds_map: dict[tuple[int, int, int], float] = {}

    tables = soup.select("table.is-w495")
    print(f"3連単オッズテーブル数: {len(tables)}（本来6）")

    for table in tables:
        current_first = None
        current_second = None
        for row in table.select("tbody tr"):
            cells = row.find_all("td")
            if not cells:
                continue

            idx = 0
            if len(cells) >= 5:
                m = re.search(r"\d", cells[0].get_text(strip=True))
                if m:
                    current_first = int(m.group())
                idx = 1
            if len(cells) - idx >= 4:
                m = re.search(r"\d", cells[idx].get_text(strip=True))
                if m:
                    current_second = int(m.group())
                idx += 1

            if current_first is None or current_second is None:
                continue

            remaining = cells[idx:]
            for i in range(0, len(remaining) - 1, 2):
                m_third = re.search(r"\d", remaining[i].get_text(strip=True))
                m_odds = re.search(r"[\d.]+", remaining[i + 1].get_text(strip=True))
                if not m_third or not m_odds:
                    continue
                try:
                    odds_val = float(m_odds.group())
                except ValueError:
                    continue
                odds_map[(current_first, current_second, int(m_third.group()))] = odds_val

    print(f"3連単オッズ取得件数: {len(odds_map)}件（本来120件）")
    return odds_map
