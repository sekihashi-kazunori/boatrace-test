// 4ページ共通の処理。データは data/ の JSON を読むだけ(サーバー側の処理なし)
const SITE_NAME = "競艇AI予想「深淵」";   // サイト名(ここだけ変えればサイト全体の表示が変わる)
const WEEK = ["日", "月", "火", "水", "木", "金", "土"];

const qs = (k) => new URLSearchParams(location.search).get(k);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function getJSON(name) {
  // 数分ごとに更新されるので、ブラウザに古い版を使わせない
  const r = await fetch(`data/${name}?t=${Math.floor(Date.now() / 60000)}`);
  if (!r.ok) throw new Error(name);
  return r.json();
}

function fmtDate(d, withWeek = true) {
  const y = +d.slice(0, 4), m = +d.slice(4, 6), day = +d.slice(6, 8);
  const w = WEEK[new Date(y, m - 1, day).getDay()];
  return withWeek ? `${m}月${day}日(${w})` : `${m}/${day}`;
}
const pct = (x, digits = 1) => (x == null ? "-" : `${(x * 100).toFixed(digits)}%`);
const pctRaw = (x) => (x == null ? "-" : `${x.toFixed(1)}%`);
const f1 = (x) => (x == null ? "-" : Number(x).toFixed(1));   // 集計済みの%(例 32 → 32.0)
const yen = (n) => (n == null ? "-" : `${n.toLocaleString()}円`);
const num = (x, d = 2) => (x == null ? "-" : Number(x).toFixed(d));
const lane = (b) => `<span class="lane l${b}">${b}</span>`;
const combo = (c) => `<span class="combo">${c.split("-").map(lane).join("")}</span>`;
// フォーメーション「12-1345-2456」を艇の色で表示
const formation = (f) => `<span class="form">${f.split("-").map((g) => `<span class="combo">${[...g].map(lane).join("")}</span>`).join('<span class="sep">-</span>')}</span>`;
// 買い目の書き方(本線+押さえ)。2026-10-17 より前の記録は本線(form)だけ
const planLines = (pl) => (pl.lines && pl.lines.length ? pl.lines : [{ form: pl.form, n: pl.n }]);
function formText(pl) {
  const ls = planLines(pl);
  return `本線 <b>${esc(ls[0].form)}</b>${ls.length > 1 ? `　押さえ ${ls.slice(1).map((l) => esc(l.form)).join("・")}` : ""}`;
}
const planHit = (race, key) => (race.result ? (race.result.plans || {})[key] : null);
const grade = (g) => (g && g !== "一般" ? `<span class="grade ${esc(g)}">${esc(g)}</span>` : "");
const hhmm = (iso) => (iso ? iso.slice(11, 16) : "-");

function resultBadge(race) {
  if (raceStatus(race) === "canceled") return `<span class="badge miss">中止</span>`;
  if (raceStatus(race) === "no_prediction") return `<span class="badge miss">予想なし</span>`;
  if (isSkipped(race)) return `<span class="badge miss">見送り</span>`;
  if (!race.result) return `<span class="badge wait">結果待ち</span>`;
  return raceHit(race) ? `<span class="badge hit">的中</span>` : `<span class="badge miss">不的中</span>`;
}

function header(active) {
  // [リンク先, 表示名, スマホでの短い表示名, キー]
  const nav = [["index.html", "今日の予想", "予想", "home"], ["performance.html", "成績", "成績", "perf"],
               ["venues.html", "会場データ", "会場", "venues"], ["about.html", "このサイトについて", "説明", "about"]];
  document.getElementById("header").innerHTML = `
    <header class="site-header"><div class="wrap">
      <a class="brand" href="index.html">${SITE_NAME}<small>直前予想</small></a>
      <nav class="nav">${nav.map(([h, t, s, k]) => `<a href="${h}" class="${k === active ? "on" : ""}"><span class="l">${t}</span><span class="s">${s}</span></a>`).join("")}</nav>
    </div></header>`;
  document.getElementById("footer").innerHTML = `
    <footer class="site-footer"><div class="wrap">
      <p>予想はAIが締切の十数分前に、展示航走などの直前情報を反映して出したものです。公開した予想は後から変更しません(公開時刻を表示しています)。</p>
      <p>的中率・回収率は過去の結果で、今後の結果を保証するものではありません。舟券の購入は20歳以上の方が、ご自身の判断と責任で行ってください。</p>
      <p>選手・レースの情報は公開情報をもとにしています。</p>
      <p class="footer-links"><a href="about.html">このサイトの考え方</a><a href="faq.html">よくある質問・用語</a><a href="performance.html">成績</a><a href="venues.html">会場データ</a><a href="legal.html">利用規約・プライバシー</a></p>
      <p>© ${new Date().getFullYear()} ${SITE_NAME}</p>
    </div></footer>`;
  document.title = document.title ? `${document.title} | ${SITE_NAME}` : SITE_NAME;
}

// 買い目の3タイプ(site_recorder.PLANS と同じ)。表示中のタイプは URL の t= で持ち回る
const PLAN_TYPES = [
  ["hit", "的中特化型", "当たる確率を最優先。AIの確率が高い順に15点"],
  ["narrow", "絞り込み型", "AIの自信が高いレースだけ。確率が高い順に6点で勝負"],
  ["shot", "一撃重視型", "本命の1着確率50%以下の荒れそうなレースだけ。高配当になる組を確率順に10点"],
];
const T = PLAN_TYPES.some(([k]) => k === qs("t")) ? qs("t") : "hit";
const T_NAME = PLAN_TYPES.find(([k]) => k === T)[1];
const tq = (key = T) => (key === "hit" ? "" : `&t=${key}`);   // リンクに付ける
// 今のページのまま、表示するタイプだけ変えたリンク
const sameWithType = (key) => {
  const q = new URLSearchParams(location.search);
  if (key === "hit") q.delete("t"); else q.set("t", key);
  return `${location.pathname.split("/").pop() || "index.html"}?${q}`;
};
const planOf = (r, key = T) => (r.plans || []).find((p) => p.key === key);
// 表示中のタイプでの的中。3タイプが無い日(サイト開始前の確認用)はAI確率上位10点で判定
// 【2026-10-21】予想をしなかったレース("no_prediction")・中止のレース("canceled")。成績には数えない
const raceStatus = (r) => (r.status === "canceled" || (r.result && r.result.canceled) ? "canceled"
  : r.status === "no_prediction" ? "no_prediction" : null);
const statusText = (r) => (raceStatus(r) === "canceled" ? (r.reason || "このレースは中止になりました。")
  : (r.reason || "このレースは予想をしていません。"));
// 【2026-10-17】絞り込み型・一撃重視型は、条件に入らないレースを「見送り」(skip)にしている
const isSkipped = (r, key = T) => { const pl = planOf(r, key); return !!(pl && pl.skip); };
// 表示中のタイプでの的中。見送りは null(的中にも不的中にも数えない)
const raceHit = (r) => (!r.result || raceStatus(r) || isSkipped(r) ? null : planOf(r) ? !!(r.result.plans || {})[T] : !!r.result.hit_rank);

// トップの冒頭に出す、このサイトのAIの説明。タイプを押すと、サイトの数字がそのタイプに切り替わる
function brandIntro(date) {
  return `<div class="brand-intro card">
    <div class="bi-copy">当たった日も、外れた日も、<br>全部見せるAI予想。</div>
    <p class="bi-lead">展示航走まで見てから、AIが全レースを予想。予想は締切前に公開して、後から変えません。結果と回収率もすべて載せます。</p>
    <div class="bi-types">${PLAN_TYPES.map(([k, n, d]) => `<a class="bi-type ${k === T ? "on" : ""}" href="index.html?d=${date}${tq(k)}"><b>${n}</b><span>${d}</span></a>`).join("")}</div>
  </div>`;
}

// 会場・レースページ用のタイプ切り替え。href(key) でリンク先を作る
function typeTabs(href) {
  return `<div class="type-tabs">${PLAN_TYPES.map(([k, n]) => `<a href="${href(k)}" class="${k === T ? "on" : ""}">${n}</a>`).join("")}</div>`;
}

// 予想のある全日付の一覧(新しい順)。スマホでは折りたたみ、広い画面では横に常に表示
function dateList(days, current) {
  const fmt = (d) => `${d.slice(0, 4)}/${+d.slice(4, 6)}/${+d.slice(6, 8)} (${WEEK[new Date(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6, 8)).getDay()]})`;
  return `<details class="datelist"><summary>日付<span class="muted">${fmt(current)}</span></summary>
    <ul>${days.map((d) => `<li><a href="index.html?d=${d.date}${tq()}" class="${d.date === current ? "on" : ""}"><span>${fmt(d.date)}</span><span class="muted">${d.races}R</span></a></li>`).join("")}</ul>
  </details>`;
}

// その日(または会場)の成績を、3タイプそれぞれで集計する
function dayStats(races) {
  races = races.filter((r) => !raceStatus(r));   // 予想なし・中止は数えない
  const done = races.filter((r) => r.result);
  const out = { races: races.length, done: done.length, honmei: 0, typed: false, plans: {} };
  for (const [k] of PLAN_TYPES) out.plans[k] = { n: 0, hits: 0, cost: 0, ret: 0, pts: 0 };
  for (const r of done) {
    out.honmei += r.result.honmei_win ? 1 : 0;
    for (const [k] of PLAN_TYPES) {
      const pl = planOf(r, k);
      if (!pl) continue;
      out.typed = true;
      if (pl.skip) continue;   // 見送りは数えない
      const a = out.plans[k];
      a.n += 1; a.pts += pl.n; a.cost += 100 * pl.n;
      if ((r.result.plans || {})[k]) { a.hits += 1; a.ret += r.result.payout; }
    }
  }
  return out;
}

// 3タイプの成績を1行ずつ並べる。表示中のタイプの行を目立たせる
function planRows(s, label) {
  const p = (a, b) => (b ? `${((100 * a) / b).toFixed(1)}%` : "-");
  if (!s.typed) {
    return comingSoon(label, s.done ? "この日は3タイプ導入前のため、成績は出していません。" : "最初のレースの結果が出しだい、ここに成績が出ます。");
  }
  return `<div class="card plan-rows">
    <div class="pr-title">${label}<span class="muted"> · 結果が出た${s.done}R</span></div>
    ${PLAN_TYPES.map(([k, n]) => {
      const a = s.plans[k];
      return `<div class="pr-row ${k === T ? "on" : ""}">
        <span class="pr-name">${n}</span><span class="muted small">${k === "hit" ? "" : `参戦${a.n}R・`}平均${a.n ? (a.pts / a.n).toFixed(1) : "-"}点</span>
        <span class="pr-num"><small>的中率</small>${p(a.hits, a.n)}</span>
        <span class="pr-num"><small>回収率</small>${p(a.ret, a.cost)}</span>
      </div>`;
    }).join("")}
    <p class="muted small">回収率は、各タイプの買い目を1点100円で買ったとした場合です。絞り込み型・一撃重視型は、買ったレース(参戦)だけで数えています。3連単は売上の約25%が差し引かれて払い戻されるため、100%を下回るのが普通です。</p>
  </div>`;
}

// まだ数字を出せない所に出す「近日公開」の枠
function comingSoon(title, body) {
  return `<div class="card coming-soon"><div class="cs-title">${title}<span class="cs-badge">近日公開</span></div><p class="muted small">${body}</p></div>`;
}

// 1レース分の買い目を、確率の高い順に縦に並べる。最初の show 点だけ見せて、残りは開いて見る
function ticketList(r, show = 5) {
  if (raceStatus(r)) return `<div class="tickets-v"><div class="skip-note">${esc(statusText(r))}</div></div>`;
  if (isSkipped(r)) {
    // 条件に入らないレースは、的中特化型の買い目を見てもらう
    const hitPlan = planOf(r, "hit");
    const why = T === "narrow" ? "AIの自信が条件に届かない" : "本命の1着確率が高い";
    return `<div class="tickets-v"><div class="skip-note">${esc(T_NAME)}は見送り(${why}レース)。<a href="${sameWithType("hit")}">的中特化型</a>の買い目を見てください。</div>
      ${hitPlan ? ticketListOf(r, hitPlan, show) : ""}</div>`;
  }
  return ticketListOf(r, planOf(r), show);
}

function ticketListOf(r, pl, show) {
  const items = pl ? pl.tickets.map((c, i) => [c, (pl.tp || [])[i]]) : (r.top || []).map((t) => [t.c, t.p]);
  const order = r.result ? r.result.order : null;
  const row = ([c, p], i) => `<li class="${c === order ? "hit" : ""}"><span class="rank">${i + 1}</span>${combo(c)}<span class="muted small">${p == null ? "" : pct(p)}</span></li>`;
  const head = pl ? `${esc(pl.name)}(${pl.n}点)　${formText(pl)}` : `AI確率上位 ${items.length}点`;
  return `<div class="tickets-v">
    <div class="tv-head">${head}</div>
    <ol>${items.slice(0, show).map(row).join("")}</ol>
    ${items.length > show ? `<details><summary>残り${items.length - show}点を見る</summary><ol start="${show + 1}">${items.slice(show).map((x, i) => row(x, i + show)).join("")}</ol></details>` : ""}
  </div>`;
}

function fail(el, msg) {
  el.innerHTML = `<div class="empty">${esc(msg)}</div>`;
}
