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


def fetch_race_result(stadium_code: str, race_number: int, target_date: Optional[date] = None) -> Optional[dict]:
    """
    指定場・指定レースの結果(着順・3連単払戻金)を取得する。

    Returns:
        {
            "stadium_code": "01",
            "race_number": 1,
            "date": "20260919",
            "finish_order": [2, 4, 3, 1, 6, 5],  # 1着から順に艇番
            "trifecta_combination": "2-4-3",
            "trifecta_payout": 8860,
            "trifecta_popularity": None,  # 何番人気だったか(取れれば)
        }
        結果未確定(まだ「データはありません」)の場合は None を返す。
    """
    if target_date is None:
        target_date = date.today()
    hd = target_date.strftime("%Y%m%d")

    url = (
        f"{BASE_URL}/owpc/pc/race/raceresult"
        f"?rno={race_number}&jcd={stadium_code}&hd={hd}"
    )
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    res.encoding = res.apparent_encoding

    soup = BeautifulSoup(res.text, "html.parser")

    # 「データはありません」= 未開催 or 結果未確定
    if "データはありません" in res.text:
        print(f"[fetch_race_result] 結果未確定 stadium_code={stadium_code} race_number={race_number} hd={hd}")
        return None

    # --- 着順テーブル ---
    finish_order: list[int] = []
    result_table = soup.select_one("table.is-w495")
    if result_table:
        for row in result_table.select("tbody tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            lane_cell = next(
                (c for c in cells if any(cls.startswith("is-boatColor") for cls in c.get("class", []))),
                None,
            )
            if lane_cell is None:
                continue
            try:
                finish_order.append(int(lane_cell.get_text(strip=True)))
            except ValueError:
                continue
    print(f"[fetch_race_result] 着順パース結果: {finish_order}")

    # --- 3連単払戻金テーブル ---
    trifecta_combination = None
    trifecta_payout = None
    trifecta_popularity = None
    payout_row = soup.select_one("tr.is-payout1, .is-p3t")
    if payout_row is None:
        # クラス名が想定と違う場合のフォールバック: 「3連単」の文字列を含む行を探す
        for row in soup.find_all("tr"):
            if "3連単" in row.get_text():
                payout_row = row
                break
    if payout_row is not None:
        text_cells = [c.get_text(strip=True) for c in payout_row.find_all("td")]
        print(f"[fetch_race_result] 3連単行の中身: {text_cells}")
        for cell in text_cells:
            if re.match(r"^\d-\d-\d$", cell):
                trifecta_combination = cell
            m_yen = re.search(r"([\d,]+)円", cell)
            if m_yen:
                trifecta_payout = int(m_yen.group(1).replace(",", ""))
            m_ninki = re.search(r"(\d+)番人気", cell)
            if m_ninki:
                trifecta_popularity = int(m_ninki.group(1))

    if not finish_order or trifecta_payout is None:
        print(f"[fetch_race_result] パース失敗の疑いあり。セレクタの調整が必要です。 url={url}")

    return {
        "stadium_code": stadium_code,
        "race_number": race_number,
        "date": hd,
        "finish_order": finish_order,
        "trifecta_combination": trifecta_combination,
        "trifecta_payout": trifecta_payout,
        "trifecta_popularity": trifecta_popularity,
    }


def fetch_before_info(stadium_code: str, race_number: int, target_date: Optional[date] = None) -> list[dict]:
    """
    指定場・指定レースの直前情報(展示タイム・チルト・ST)を取得する。

    Returns:
        [{"lane_number": 1, "exhibition_time": 6.78, "tilt": -0.5, "start_timing": 0.15}, ...]
        直前情報がまだ反映されていない場合は空リストを返す。
    """
    if target_date is None:
        target_date = date.today()
    hd = target_date.strftime("%Y%m%d")

    url = (
        f"{BASE_URL}/owpc/pc/race/beforeinfo"
        f"?rno={race_number}&jcd={stadium_code}&hd={hd}"
    )
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    res.encoding = res.apparent_encoding

    soup = BeautifulSoup(res.text, "html.parser")
    before_entries: list[dict] = []

    table = soup.select_one("table.is-w748")
    if table is None:
        print(f"[fetch_before_info] 直前情報テーブルが見つかりません(未反映の可能性) url={url}")
        return before_entries

    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        lane_cell = next(
            (c for c in cells if any(cls.startswith("is-boatColor") for cls in c.get("class", []))),
            None,
        )
        if lane_cell is None:
            continue
        try:
            lane = int(lane_cell.get_text(strip=True))
        except ValueError:
            continue

        row_text = [c.get_text(strip=True) for c in cells]
        print(f"[fetch_before_info] {lane}号艇 行の中身: {row_text}")

        exhibition_time = None
        tilt = None
        start_timing = None
        for cell_text in row_text:
            try:
                val = float(cell_text)
            except ValueError:
                continue
            if 6.0 <= val <= 8.0 and exhibition_time is None:
                exhibition_time = val
            elif -1.0 <= val <= 1.0 and tilt is None:
                tilt = val
            elif 0.0 <= val <= 1.0 and start_timing is None:
                start_timing = val

        before_entries.append({
            "lane_number": lane,
            "exhibition_time": exhibition_time,
            "tilt": tilt,
            "start_timing": start_timing,
        })

    return before_entries

    
     
