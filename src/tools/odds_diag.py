"""過去オッズ取得の速度・中身の診断。結果を odds_diag.txt に書く。"""
import time
from datetime import date

import requests

from src.collectors.official_site import HEADERS, fetch_odds_3rentan

out = []
for d, st, rn in ((date(2026, 9, 18), "01", 1), (date(2026, 9, 18), "12", 5), (date(2026, 10, 11), "23", 5)):
    url = f"https://www.boatrace.jp/owpc/pc/race/odds3t?rno={rn}&jcd={st}&hd={d:%Y%m%d}"
    t = time.time()
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        out.append(f"raw {d} {st} {rn}R status={r.status_code} len={len(r.text)} {time.time()-t:.1f}s")
    except Exception as e:
        out.append(f"raw {d} {st} {rn}R ERROR {e!r} {time.time()-t:.1f}s")
    t = time.time()
    try:
        o = fetch_odds_3rentan(st, rn, d)
        out.append(f"lib {d} {st} {rn}R n={len(o)} 1-2-3={o.get((1,2,3))} {time.time()-t:.1f}s")
    except Exception as e:
        out.append(f"lib {d} {st} {rn}R ERROR {e!r} {time.time()-t:.1f}s")
open("odds_diag.txt", "w").write("\n".join(out) + "\n")
print("\n".join(out))
