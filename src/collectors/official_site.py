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
        [{"stadium_code": "01", "stadium_name": "桐生", "race_count": 12}, ...]
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
    for link in soup.select("a[href*='racelist'][href*='jcd=']"):
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
    rows = soup.select("tbody.is-fs12")
    for idx, row in enumerate(rows[:6], start=1):
        name_tag = row.select_one(".is-fs18 a")
        name = name_tag.get_text(strip=True) if name_tag else None

        number_tag = row.select_one(".is-fs11")
        racer_number = None
        if number_tag:
            m = re.search(r"\d{4}", number_tag.get_text())
            if m:
                racer_number = m.group(0)

        racers.append({
            "lane": idx,
            "name": name,
            "number": racer_number,
        })

    return {
        "stadium_code": stadium_code,
        "race_number": race_number,
        "date": hd,
        "racers": racers,
    }
