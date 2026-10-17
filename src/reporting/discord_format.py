"""Discord向けの収支表示(2026-10-17 ご主人様指示で「見やすく・分析しやすく」作り直し)。

DBに触らない純粋な整形関数だけを置く。数字は report.py / settle_pipeline.py が集めて渡す。
表は ``` で囲んだ等幅表示にして、スマホでも列がそろうように全角は2文字幅で数える。
"""
from __future__ import annotations

import unicodedata


def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WFA" else 1 for c in s)


def _pad(s: str, n: int, left: bool = False) -> str:
    sp = " " * max(0, n - _w(s))
    return s + sp if left else sp + s


def yen(v: int) -> str:
    return f"{v:+,}" if v else "±0"


def mark(profit: int, races: int) -> str:
    if not races:
        return "➖"
    return "✅" if profit > 0 else ("➖" if profit == 0 else "🔻")


def table(header: list[str], rows: list[list[str]]) -> str:
    """1列目は左寄せ、残りは右寄せの等幅表。"""
    cols = list(zip(header, *[r for r in rows if r and r[0] != "__sep__"]))
    widths = [max(_w(c) for c in col) for col in cols]

    def line(cells):
        return " ".join(_pad(c, widths[i], left=(i == 0 or (header[-1] == "" and i == len(cells) - 1))) for i, c in enumerate(cells)).rstrip()

    rule = "-" * (sum(widths[:-1]) + len(widths) - 2 if header[-1] == "" else sum(widths) + len(widths) - 1)
    out = [line(header), rule]
    for r in rows:
        if r and r[0] == "__sep__":
            out.append(rule)
            continue
        out.append(line(r))
    return "```\n" + "\n".join(out) + "\n```"


def stat_row(label: str, s: dict) -> list[str]:
    """s: race_count, hit_race_count, profit, recovery_rate"""
    n = s["race_count"]
    if not n:
        return [label, "0", "-", "-", "-"]
    return [label, str(n), f"{s['hit_race_count']}({s['hit_race_count'] / n * 100:.0f}%)",
            yen(s["profit"]), f"{s['recovery_rate']:.0f}%"]


STAT_HEADER = ["区分", "R", "的中", "収支", "回収"]


def session_message(session_ja: str, md: str, s: dict, races: list[dict]) -> str:
    """セッション終了ごとの本予想報告。
    races: [{"name": "江戸川3R", "order": "123", "stake": 1400, "payout": 5600, "status": "hit|miss|wait"}]"""
    lines = [f"**📊 {session_ja} 本予想 {md}**"]
    if not s["race_count"]:
        lines.append("対象レースなし")
        return "\n".join(lines)
    lines.append(f"{mark(s['profit'], s['race_count'])} 収支 **{yen(s['profit'])}円** / 回収率 **{s['recovery_rate']:.0f}%**")
    lines.append(f"的中 {s['hit_race_count']}/{s['race_count']}R ({s['hit_rate']:.0f}%)"
                 f" / 購入 {s['total_stake']:,} → 払戻 {s['total_payout']:,}円")
    if races:
        rows = []
        # 絵文字は幅がずれるので行末に置く
        for r in races:
            if r["status"] == "wait":
                rows.append([r["name"], "待ち", f"{r['stake']:,}", "-", "⏳"])
                continue
            pr = r["payout"] - r["stake"]
            icon = ("🎯" if pr > 0 else "🎯ガミ") if r["status"] == "hit" else ""
            rows.append([r["name"], r["order"] or "-", f"{r['stake']:,}", yen(pr), icon])
        lines.append(table(["レース", "結果", "購入", "収支", ""], rows))
    return "\n".join(lines)


def final_message(md: str, today: list[tuple[str, dict]], today_total: dict,
                  cum: list[tuple[str, dict]], cum_total: dict, since_md: str,
                  daily: list[tuple[str, dict]]) -> str:
    """ナイター後の最終振り返り(本予想)。
    today/cum: [("朝", summary), ("昼", ...), ("夜", ...)]  daily: [("10/14", summary), ...]"""
    lines = [f"**📅 本日の総収支 本予想 {md}**",
             f"{mark(today_total['profit'], today_total['race_count'])} 合計 **{yen(today_total['profit'])}円**"
             f" / 回収率 **{today_total['recovery_rate']:.0f}%**"
             f" (購入 {today_total['total_stake']:,} → 払戻 {today_total['total_payout']:,}円)"]
    lines.append(table(STAT_HEADER, [stat_row(l, s) for l, s in today]
                       + [["__sep__"], stat_row("合計", today_total)]))

    lines.append(f"**📈 累積 本予想 {since_md}〜**")
    lines.append(f"{mark(cum_total['profit'], cum_total['race_count'])} 累積 **{yen(cum_total['profit'])}円**"
                 f" / 回収率 **{cum_total['recovery_rate']:.0f}%**")
    lines.append(table(STAT_HEADER, [stat_row(l, s) for l, s in cum]
                       + [["__sep__"], stat_row("合計", cum_total)]))

    if daily:
        rows, run = [], 0
        for d, s in daily:
            run += s["profit"]
            rows.append([d, str(s["race_count"]), f"{s['recovery_rate']:.0f}%", yen(s["profit"]), yen(run)])
        lines.append("**🗓 日別の推移**")
        lines.append(table(["日付", "R", "回収", "当日", "累積"], rows))

    # 一番強い/弱い時間帯をひとこと(累積・3R以上ある区分だけ)
    ok = [(l, s) for l, s in cum if s["race_count"] >= 3]
    if len(ok) >= 2:
        best = max(ok, key=lambda x: x[1]["recovery_rate"])
        worst = min(ok, key=lambda x: x[1]["recovery_rate"])
        lines.append(f"💡 累積では {best[0]}が最も強く({best[1]['recovery_rate']:.0f}%)、"
                     f"{worst[0]}が最も弱い({worst[1]['recovery_rate']:.0f}%)")
    return "\n".join(lines)


def verify_message(since_md: str, rule_text: str, items: list[tuple[str, dict, bool]], extra: str = "") -> str:
    """検証枠の累積。items: [(表示名, {"races","hit_races","stake","payout","recovery_rate"}, 昇格OK)]
    回収率の高い順に並べ、対象なしは最後にまとめる。"""
    lines = [f"**🧪 検証枠 累積** ※紙上 {since_md}〜", rule_text]
    active = sorted([x for x in items if x[1]["races"]], key=lambda x: -x[1]["recovery_rate"])
    idle = [x[0] for x in items if not x[1]["races"]]
    for title, r, ok in active:
        icon = "🎉" if ok else mark(r["payout"] - r["stake"], r["races"])
        lines.append(f"{icon} **{title}**")
        lines.append(f"　{r['races']}R 的中{r['hit_races']} │ 購入{r['stake']:,}円 → 払戻{r['payout']:,}円")
        lines.append(f"　収支 {yen(r['payout'] - r['stake'])}円 │ 回収率 {r['recovery_rate']:.0f}%")
        # 2026-10-17: 万舟頼みかどうかを見るため、一番大きい当たり1本を抜いた回収率も出す
        mx = r.get("max_race_payout", 0)
        if r["hit_races"] >= 1 and r["stake"]:
            lines.append(f"　最大の当たり{mx:,}円を抜くと 回収率 {(r['payout'] - mx) / r['stake'] * 100:.0f}%")
    st, py = sum(x[1]["stake"] for x in active), sum(x[1]["payout"] for x in active)
    if st:
        lines.append(f"📦 検証枠 合計: 購入{st:,}円 → 払戻{py:,}円 / 収支 {yen(py - st)}円 / 回収率 {py / st * 100:.0f}%")
    if idle:
        lines.append("➖ まだ対象なし: " + "、".join(idle))
    if extra:
        lines.append(extra)
    promoted = [t for t, _, ok in items if ok]
    if promoted:
        lines.append("👉 昇格候補: " + "、".join(promoted) + "（執事に本予想への昇格を指示してください）")
    return "\n".join(lines)
