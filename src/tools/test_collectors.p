"""
収集関数の動作確認用スクリプト。
モデルの有無に関係なく、fetch_race_card / fetch_before_info / fetch_race_result
の3つだけを直接呼び出して結果をログに出す。

引数を省略した場合は「昨日」の桐生(場コード01) 1Rをテストする。
"""
import argparse
from datetime import date, timedelta

from src.collectors.official_site import fetch_race_card, fetch_race_result, fetch_before_info


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, default=None, help="YYYYMMDD形式。省略時は昨日")
    parser.add_argument("--jcd", type=str, default="01", help="場コード。省略時は01(桐生)")
    parser.add_argument("--rno", type=int, default=1, help="レース番号。省略時は1")
    args = parser.parse_args()

    if args.date:
        target_date = date(int(args.date[:4]), int(args.date[4:6]), int(args.date[6:8]))
    else:
        target_date = date.today() - timedelta(days=1)

    print(f"=== テスト対象: {target_date} 場コード={args.jcd} {args.rno}R ===")

    print("\n--- fetch_race_card ---")
    try:
        card = fetch_race_card(args.jcd, args.rno, target_date)
        print(card)
    except Exception as e:
        print(f"fetch_race_card 失敗: {e}")

    print("\n--- fetch_before_info ---")
    try:
        before = fetch_before_info(args.jcd, args.rno, target_date)
        print(before)
    except Exception as e:
        print(f"fetch_before_info 失敗: {e}")

    print("\n--- fetch_race_result ---")
    try:
        result = fetch_race_result(args.jcd, args.rno, target_date)
        print(result)
    except Exception as e:
        print(f"fetch_race_result 失敗: {e}")


if __name__ == "__main__":
    main()
