"use strict";

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";
const state = { meta: null, horizon: "10", range: 250, product: null, rtTimer: null, rtLast: null };

// ---------- 小工具 ----------
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null && c !== false) node.append(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  return node;
}
function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}
function svgText(x, y, str, attrs = {}) { const t = svgEl("text", { x, y, ...attrs }); t.textContent = str; return t; }
const num = (n, d = 0) => n == null ? "—" : Number(n).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (p, d = 0) => p == null ? "—" : (p * 100).toFixed(d) + "%";
const signed = (p, d = 1) => p == null ? "—" : (p > 0 ? "+" : "") + (p * 100).toFixed(d) + "%";
// 价格按最小变动价位显示，焦煤 1414.5 这种不能四舍五入成 1,415
const px = (v) => num(v, v == null || Number.isInteger(Number(v)) ? 0 : 1);
const signedPx = (v) => v == null ? "—" : (v > 0 ? "+" : "") + px(v);
// 和行情软件一致的口径说明（总览和品种页共用）
const priceNote = (d) => `价格是 ${d} 日盘收盘（15:00），不含当晚 21:00 开始的夜盘（夜盘算下一个交易日）；涨跌按昨结算算，和行情软件一致。`;
const chgClass = (v) => v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : "";
// ---------- 实时行情（新浪，约 1 分钟更新；只用来看行情，不改变信号） ----------
const RT_NOTE = "实时行情是新浪的最新成交价，约 1 分钟更新，涨跌同样比昨结算；只用来看行情，不会改变当天的信号。";
const rtTime = (q) => q.live ? `${q.session}交易中 · ${q.time.slice(11, 19)}` : `休市 · 最后成交 ${q.time.slice(5, 10)} ${q.time.slice(11, 16)}`;
function rtCell(q) {
  if (!q) return ["—"];
  return [el("div", {}, el("b", {}, px(q.last)), " ", el("span", { class: chgClass(q.change_settle) }, signed(q.change_settle, 2))),
          el("div", { class: "muted small" }, q.live ? `${q.session} ${q.time.slice(11, 16)}` : `休市 ${q.time.slice(5, 10)} ${q.time.slice(11, 16)}`)];
}
async function loadQuotes() {
  try { return await api("/api/quotes"); } catch (err) { return { quotes: {}, error: err.message }; }
}
// 打开页面时取一次，之后每分钟刷新；页面在后台时跳过
function startQuotes(paint) {
  clearInterval(state.rtTimer);
  const tick = async () => { if (document.hidden) return; state.rtLast = await loadQuotes(); paint(state.rtLast); };
  tick();
  state.rtTimer = setInterval(tick, 60000);
}

function md(iso) { const [, m, d] = iso.split("-").map(Number); return `${m}/${d}`; }
function ym(iso) { const [y, m] = iso.split("-"); return `${y.slice(2)}/${m}`; }

async function api(url, opts) {
  const resp = await fetch(url, opts);
  let data = null;
  try { data = await resp.json(); } catch { /* 非 JSON */ }
  if (resp.status === 401 && !url.endsWith("/login")) { showLogin(); throw new Error("请先输入访问口令。"); }
  if (!resp.ok) throw new Error((data && data.detail) || `请求失败（${resp.status}）`);
  return data;
}

function niceTicks(lo, hi, count) {
  const raw = (hi - lo) / Math.max(count, 1);
  const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const step = [1, 2, 2.5, 5, 10].map((s) => s * mag).find((s) => s >= raw) || 10 * mag;
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) ticks.push(Math.round(v / step) * step);
  return ticks;
}
function showError(msg) { $("error").textContent = msg; $("error").hidden = !msg; }

function dirClass(d) {
  if (!d) return "";
  if (d.startsWith("偏多")) return "long";
  if (d.startsWith("偏空")) return "short";
  return "flat";
}
function signalBadge(v) {
  if (!v) return el("span", { class: "muted" }, "—");
  return el("span", { class: `sig ${dirClass(v.direction)} conf-${v.confidence}` },
    el("b", {}, v.direction.replace("（未校准）", "")), " ", el("span", { class: "sig-sub" }, `${pct(v.prob)} · ${v.confidence}`));
}

// ---------- 登录 ----------
function showLogin() {
  $("login").hidden = false; $("view").replaceChildren(); $("nav").hidden = true;
  setTimeout(() => $("passcode").focus(), 0);
}
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").hidden = true;
  try {
    await api("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: $("passcode").value }) });
    $("login").hidden = true; $("passcode").value = "";
    route();
  } catch (err) { $("login-error").textContent = err.message; $("login-error").hidden = false; }
});
$("logout").addEventListener("click", async () => { await fetch("/api/logout", { method: "POST" }); showLogin(); });

// ---------- 路由 ----------
async function route() {
  showError("");
  $("tooltip").hidden = true;
  clearInterval(state.rtTimer); state.rtLast = null;
  const h = location.hash.replace(/^#\/?/, "");
  try {
    $("nav").hidden = false;
    if (h.startsWith("p/")) await renderProduct(h.slice(2).toUpperCase());
    else if (h === "backtest") await renderBacktest();
    else if (h === "health") await renderHealth();
    else await renderOverview();
    for (const a of $("nav").querySelectorAll("a")) a.classList.toggle("active", a.getAttribute("href") === "#/" + (h.startsWith("p/") ? "" : h));
  } catch (err) { if (!$("login").hidden) return; showError(err.message); }
}
window.addEventListener("hashchange", route);

function setStamp(meta) {
  state.meta = meta;
  $("disclaimer").textContent = meta.disclaimer;
  $("stamp").textContent = `信号日 ${meta.signal_day} · 行情截至 ${meta.data_last_date} · 计算于 ${meta.generated_at.replace("T", " ").slice(0, 16)}（北京时间）`;
}

// ---------- 总览 ----------
async function renderOverview() {
  const data = await api("/api/overview");
  setStamp(data.meta);
  const hs = data.meta.horizons.map(String);
  const bySector = {};
  for (const p of data.products) (bySector[p.sector] ||= []).push(p);
  const intro = el("section", { class: "card intro" },
    el("h3", {}, "怎么看"),
    el("p", { class: "muted small" },
      "每个品种给出未来 5 / 10 / 20 个交易日的信号方向和校准后的上涨概率。可信度由历史样本外表现决定："
      + "历史上在同一段行情里分辨涨跌的能力不比随机猜测好，就标“低”；临近重大事件、换月、波动剧烈时还会再下调。"
      + "颜色按国内习惯：红色偏多，绿色偏空。"),
    el("p", { class: "muted small" }, priceNote(data.meta.data_last_date) + "5 日涨跌是收盘对收盘、换月已复权。"),
    el("p", { class: "muted small", id: "rt-note" }, RT_NOTE));
  const cards = Object.entries(data.meta.sectors).map(([sector, codes]) => {
    const rows = codes.map((c) => bySector[sector]?.find((p) => p.product === c)).filter(Boolean).map((p) =>
      el("tr", { class: "clickable", onclick: () => { location.hash = `#/p/${p.product}`; }, tabindex: 0,
                 onkeydown: (e) => { if (e.key === "Enter") location.hash = `#/p/${p.product}`; } },
        el("td", {}, el("b", {}, p.name), el("span", { class: "muted small" }, " " + p.contract)),
        el("td", { class: "num" }, px(p.close)),
        el("td", { class: "num opt" }, px(p.settle)),
        el("td", { class: "num " + chgClass(p.change_settle) }, signed(p.change_settle, 2)),
        el("td", { class: "num opt " + chgClass(p.change_5d) }, signed(p.change_5d)),
        el("td", { class: "num rt", id: `rt-${p.product}` }, el("span", { class: "muted small" }, "加载中…")),
        ...hs.map((h) => el("td", {}, signalBadge(p.horizons[h]))),
        // 括号里的解释放到悬停提示；屏幕较窄时这两列隐藏（品种页上都有），表格不出横向滚动条
        el("td", { class: "opt wide small muted", title: p.quadrant }, p.quadrant.split("（")[0]),
        el("td", { class: "opt wide small muted" }, p.downgrades.length ? `${p.downgrades.length} 项提示` : "")));
    return el("section", { class: "card" },
      el("div", { class: "card-head" }, el("h3", {}, sector)),
      el("div", { class: "table-scroll" }, el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "品种"), el("th", { class: "num" }, `${data.meta.data_last_date.slice(5)} 收盘`), el("th", { class: "num opt" }, "结算"),
          el("th", { class: "num", title: "比昨结算" }, "涨跌"),
          el("th", { class: "num opt" }, "5 日"), el("th", { class: "num rt" }, "实时最新"), ...hs.map((h) => el("th", {}, `${h} 日信号`)), el("th", { class: "opt wide" }, "量价仓"),
          el("th", { class: "opt wide" }, ""))),
        el("tbody", {}, ...rows))));
  });
  $("view").replaceChildren(intro, ...cards);
  startQuotes((d) => {
    for (const p of data.products) $(`rt-${p.product}`)?.replaceChildren(...rtCell(d.quotes[p.product]));
    $("rt-note").textContent = RT_NOTE + (d.error ? `（暂时取不到新价格：${d.error}，显示的是上一次取到的）` : "");
  });
}

// ---------- 通用折线图 ----------
// opts: dates, lines: [{label, values, cls}], area: {label, values}, markers: [i], ref, fmt, height, yLabel
function lineChart(box, opts) {
  const { dates } = opts;
  const n = dates.length;
  const W = Math.max(box.clientWidth, 300);
  const hasArea = !!opts.area;
  const H = opts.height || (hasArea ? 330 : 240);
  const m = { l: 58, r: 14, t: 14, b: 26 };
  const mainH = hasArea ? (H - m.t - m.b) * 0.72 : H - m.t - m.b;
  const areaTop = m.t + mainH + 12;
  const areaH = H - m.b - areaTop;
  const fmt = opts.fmt || ((v) => num(v));
  const vals = opts.lines.flatMap((l) => l.values).filter((v) => v != null && isFinite(v));
  if (opts.ref != null) vals.push(opts.ref);
  if (!vals.length) { box.replaceChildren(el("p", { class: "muted small" }, "暂无数据")); return; }
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.06 || Math.abs(hi) * 0.05 || 1;
  lo -= pad; hi += pad;
  const x = (i) => m.l + (i / Math.max(n - 1, 1)) * (W - m.l - m.r);
  const y = (v) => m.t + ((hi - v) / (hi - lo)) * mainH;
  const s = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" });
  const grid = svgEl("g", { class: "grid" });
  const ticks = niceTicks(lo, hi, 4).filter((v) => v >= lo && v <= hi);
  for (const v of ticks) { grid.append(svgEl("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) })); s.append(svgText(m.l - 8, y(v) + 4, fmt(v), { "text-anchor": "end" })); }
  s.prepend(grid);
  if (opts.ref != null) s.append(svgEl("line", { class: "ref-line", x1: m.l, x2: W - m.r, y1: y(opts.ref), y2: y(opts.ref) }));
  for (const i of opts.markers || []) s.append(svgEl("line", { class: "marker-line", x1: x(i), x2: x(i), y1: m.t, y2: m.t + mainH }));
  const nT = W < 520 ? 4 : 7;
  for (let k = 0; k < nT; k++) {
    const i = Math.round((k / (nT - 1)) * (n - 1));
    s.append(svgText(x(i), H - 6, n > 300 ? ym(dates[i]) : md(dates[i]), { "text-anchor": k === 0 ? "start" : k === nT - 1 ? "end" : "middle" }));
  }
  const path = (values, yf) => {
    let d = "", pen = false;
    values.forEach((v, i) => { if (v == null || !isFinite(v)) { pen = false; return; } d += (pen ? "L" : "M") + x(i).toFixed(1) + "," + yf(v).toFixed(1); pen = true; });
    return d;
  };
  opts.lines.forEach((l, k) => s.append(svgEl("path", { class: `line ${l.cls || "s" + (k + 1)}`, d: path(l.values, y) })));
  let ya = null;
  if (hasArea) {
    const av = opts.area.values.filter((v) => v != null);
    const alo = Math.min(...av) * 0.98, ahi = Math.max(...av) * 1.02;
    ya = (v) => areaTop + ((ahi - v) / (ahi - alo || 1)) * areaH;
    const top = opts.area.values.map((v, i) => `${x(i).toFixed(1)},${ya(v ?? alo).toFixed(1)}`).join("L");
    s.append(svgEl("path", { class: "area", d: `M${x(0)},${areaTop + areaH}L${top}L${x(n - 1)},${areaTop + areaH}Z` }));
    s.append(svgText(m.l - 8, areaTop + 10, opts.area.short || "", { "text-anchor": "end", class: "label-2" }));
  }
  // 悬停
  const cross = svgEl("line", { class: "crosshair", y1: m.t, y2: H - m.b, visibility: "hidden" });
  const hit = svgEl("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" });
  s.append(cross, hit);
  const show = (e) => {
    const rect = s.getBoundingClientRect();
    const i = Math.max(0, Math.min(n - 1, Math.round(((e.clientX - rect.left - m.l) / (W - m.l - m.r)) * (n - 1))));
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    const rows = opts.lines.map((l, k) => el("div", { class: "tt-row" }, el("span", { class: `tt-key ${l.cls || "s" + (k + 1)}` }), el("b", {}, l.values[i] == null ? "—" : fmt(l.values[i])), el("span", { class: "muted" }, l.label)));
    if (hasArea) rows.push(el("div", { class: "tt-row" }, el("span", { class: "tt-key area-key" }), el("b", {}, num(opts.area.values[i])), el("span", { class: "muted" }, opts.area.label)));
    if (opts.extra) rows.push(...opts.extra(i));
    const tip = $("tooltip");
    tip.replaceChildren(el("div", { class: "tt-title" }, dates[i]), ...rows);
    tip.hidden = false;
    let left = e.clientX + 14, top = e.clientY - tip.offsetHeight - 12;
    if (left + tip.offsetWidth > innerWidth - 8) left = e.clientX - tip.offsetWidth - 14;
    if (top < 8) top = e.clientY + 16;
    tip.style.left = `${Math.max(8, left)}px`; tip.style.top = `${top}px`;
  };
  hit.addEventListener("pointermove", show);
  hit.addEventListener("pointerdown", show);
  hit.addEventListener("pointerleave", (e) => { if (e.pointerType !== "touch") { cross.setAttribute("visibility", "hidden"); $("tooltip").hidden = true; } });
  box.replaceChildren(s);
}

function legend(items) {
  return el("ul", { class: "legend" }, ...items.map(([cls, label]) => el("li", {}, el("span", { class: `key ${cls}` }), label)));
}
function slice(arr, k) { return arr ? arr.slice(-k) : []; }

// ---------- 品种页 ----------
async function renderProduct(code) {
  const data = await api(`/api/product/${code}`);
  setStamp(data.meta);
  state.product = data;
  drawProduct();
  startQuotes(paintProductQuotes);
}

function paintProductQuotes(d) {
  const s = state.product?.signal, q = d?.quotes[s?.product];
  if (!s || !$("rt-status")) return;
  $("rt-status").textContent = (q ? rtTime(q) : "暂时取不到实时行情") + (d.error ? `（${d.error}）` : "");
  $("rt-tiles").replaceChildren(...(q ? [
    tile("最新价", px(q.last), `${q.contract} · ${s.unit}`),
    tile("涨跌（比昨结算）", `${signedPx(q.change_pts)} / ${signed(q.change_settle, 2)}`, `昨结算 ${px(q.pre_settle)}`, chgClass(q.change_settle)),
    tile("今开", px(q.open), `最高 ${px(q.high)} · 最低 ${px(q.low)}`),
    tile("成交量 / 持仓量", `${num(q.volume)} / ${num(q.oi)}`, "本交易日（含夜盘）累计")] : []));
}

function drawProduct() {
  const { signal: s, series, news, explanation } = state.product;
  const H = state.horizon;
  const v = s.horizons[H];
  const tabs = el("div", { class: "segmented", role: "tablist" }, ...Object.keys(s.horizons).map((h) =>
    el("button", { class: h === H ? "seg on" : "seg", role: "tab", "aria-selected": h === H ? "true" : "false",
                   onclick: () => { state.horizon = h; drawProduct(); } }, `${h} 日`)));

  const head = el("section", { class: "card" },
    el("div", { class: "card-head" },
      el("div", {}, el("h2", {}, `${s.name} `, el("span", { class: "muted" }, s.contract)),
        el("p", { class: "muted small" }, `${s.exchange} · ${s.sector} · ${s.date} 日盘收盘，不含夜盘`)),
      tabs),
    el("div", { class: "tiles" },
      tile("收盘", px(s.close), `${s.date} 15:00 · ${s.unit}`),
      tile("结算价", px(s.settle), `昨结算 ${px(s.pre_settle)}`),
      tile("涨跌（比昨结算）", `${signedPx(s.change_pts)} / ${signed(s.change_settle, 2)}`, "和行情软件一致", chgClass(s.change_settle)),
      tile("5 日 / 20 日", `${signed(s.change_5d)} / ${signed(s.change_20d)}`, "收盘对收盘，换月已复权"),
      tile("持仓量", num(s.total_oi), `5 日 ${signed(s.oi_change_5d)}`),
      tile("量价仓", s.quadrant.split("（")[0], s.quadrant.split("（")[1]?.replace("）", "") || "")),
    el("p", { class: "muted small" }, priceNote(s.date)));

  const verdict = v ? el("section", { class: `card verdict ${dirClass(v.direction)}` },
    el("div", { class: "verdict-head" },
      el("div", {}, el("span", { class: "muted small" }, `未来 ${H} 个交易日（至 ${v.until}）`),
        el("div", { class: "verdict-title" }, `信号${v.direction}`, el("span", { class: `conf conf-${v.confidence}` }, `可信度 ${v.confidence}`))),
      el("div", { class: "hero" }, el("span", { class: "hero-label" }, "校准后上涨概率"), el("span", { class: "hero-value" }, pct(v.prob)))),
    el("h4", {}, "可靠性依据"),
    el("ul", { class: "reasons" }, ...v.reasons.map((r) => el("li", {}, r))),
    v.downgrades.length ? el("div", {}, el("h4", {}, "降级提示"), el("ul", { class: "reasons warn" }, ...v.downgrades.map((r) => el("li", {}, r)))) : null,
    v.analogs ? el("p", { class: "small muted analog" },
      `历史相似情形 ${v.analogs.k} 段（彼此间隔至少 ${H} 个交易日，分布在 ${v.analogs.years[0]}~${v.analogs.years.at(-1)} 年）：之后 ${H} 日上涨 ${pct(v.analogs.up_share)}，`
      + `收益中位数 ${signed(v.analogs.median_ret)}，四分位 ${signed(v.analogs.p25)} ~ ${signed(v.analogs.p75)}。`) : null)
    : el("section", { class: "card" }, el("p", { class: "muted" }, "这个周期还没有信号。"));

  const expl = explanation ? el("section", { class: "card" },
    el("div", { class: "card-head" }, el("h3", {}, "解读"), el("span", { class: "badge" }, explanation.source === "claude" ? "Claude 撰写（数字来自模型结果）" : "模板生成")),
    ...renderText(explanation.text)) : null;

  // 价格与持仓
  const k = state.range;
  const dates = slice(series.date, k);
  const rollIdx = slice(series.roll_day, k).map((r, i) => (r ? i : -1)).filter((i) => i >= 0);
  const priceCard = el("section", { class: "card" },
    el("div", { class: "card-head" }, el("h3", {}, "主力合约价格与总持仓"),
      el("div", { class: "segmented" }, ...[[120, "半年"], [250, "1 年"], [500, "2 年"]].map(([d, label]) =>
        el("button", { class: d === k ? "seg on" : "seg", onclick: () => { state.range = d; drawProduct(); } }, label)))),
    legend([["s1", "主力收盘（原始价）"], ["area-key", "全部合约持仓量"], ["marker", "主力换月"]]),
    el("div", { class: "chart", id: "chart-price" }));
  const probCard = el("section", { class: "card" },
    el("h3", {}, "历史信号（样本外上涨概率）"),
    el("p", { class: "muted small" }, "每一天的概率都只用了当时已知的数据；高于 55% 记为偏多，低于 45% 记为偏空。"),
    legend(Object.keys(s.horizons).map((h, i) => [`s${i + 1}`, `${h} 日`])),
    el("div", { class: "chart", id: "chart-prob" }));

  // 因子贡献
  const contrib = v ? el("section", { class: "card" },
    el("h3", {}, `${H} 日打分的因子贡献`),
    el("p", { class: "muted small" }, "贡献 = 因子 z 值 × 滚动样本外 IC 权重（已归一化）。胜率是这个因子单独按当时权重方向判断、在 |z| ≥ 0.5 的日子里的样本外胜率。"),
    el("div", { class: "table-scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "因子"), el("th", { class: "opt" }, "分组"), el("th", { class: "num" }, "z 值"),
        el("th", {}, "贡献"), el("th", { class: "num" }, "历史胜率"), el("th", { class: "num opt" }, "样本"))),
      el("tbody", {}, ...v.contributions.slice(0, 14).map((c) => el("tr", {},
        el("td", {}, c.label), el("td", { class: "opt muted small" }, c.group), el("td", { class: "num" }, c.z == null ? "—" : c.z.toFixed(2)),
        el("td", {}, contribBar(c.contribution)), el("td", { class: "num" }, pct(c.hit_rate)), el("td", { class: "num opt muted" }, c.n ?? "—"))))))) : null;

  const groups = {};
  for (const f of s.factors) (groups[f.group] ||= []).push(f);
  const factorCard = el("section", { class: "card" },
    el("h3", {}, "全部因子当前值"),
    el("div", { class: "table-scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "因子"), el("th", { class: "num" }, "原始值"), el("th", { class: "num" }, "z 值"),
        el("th", { class: "num opt" }, "一年分位"), el("th", { class: "opt" }, "说明"))),
      el("tbody", {}, ...Object.entries(groups).flatMap(([g, fs]) => [
        el("tr", { class: "group-row" }, el("td", { colspan: 5 }, g)),
        ...fs.map((f) => el("tr", {}, el("td", {}, f.label, f.in_model ? "" : el("span", { class: "muted small" }, "（不计分）")),
          el("td", { class: "num" }, f.raw == null ? "—" : Math.abs(f.raw) >= 100 ? num(f.raw) : f.raw.toFixed(4)),
          el("td", { class: "num" }, f.z == null ? "—" : f.z.toFixed(2)), el("td", { class: "num opt" }, pct(f.pct)),
          el("td", { class: "opt small muted wrapcell" }, f.desc)))])))));

  const extCards = [];
  if (s.foreign && series.ext_ratio) {
    extCards.push(el("section", { class: "card" }, el("h3", {}, `内外盘：${s.foreign.name}`),
      el("p", { class: "muted small" }, `国内价格 ÷（外盘 × 美元汇率）的对数；外盘和汇率取前一个交易日（伦敦当天还没收盘）。当前偏离 z = ${s.foreign.ratio_z ?? "—"}。`),
      legend([["s1", "内外比价（对数）"]]), el("div", { class: "chart", id: "chart-ext" })));
  }
  const spreadKeys = [["spread_rb", "螺纹盘面利润"], ["spread_hc", "热卷盘面利润"], ["spread_coke", "焦化盘面利润"]].filter(([key]) => series[key]);
  if (spreadKeys.length) {
    extCards.push(el("section", { class: "card" }, el("h3", {}, "产业链盘面利润（近似）"),
      el("p", { class: "muted small" }, (state.meta.spreads || []).filter((sp) => spreadKeys.some(([, n]) => n === sp.name)).map((sp) => sp.note).join("；")),
      legend(spreadKeys.map(([, label], i) => [`s${i + 1}`, label])), el("div", { class: "chart", id: "chart-spread" })));
  }
  if (series.net_long && series.net_long.some((x) => x != null)) {
    extCards.push(el("section", { class: "card" }, el("h3", {}, "前 20 名会员净多比例"),
      el("p", { class: "muted small" }, s.exchange === "DCE" ? "大商所品种只统计主力合约（新浪数据）。" : "上期所全部合约前 20 名合计。"),
      el("div", { class: "chart", id: "chart-net" })));
  }

  const newsCard = renderNews(news, s);
  const events = v && v.events.length ? el("section", { class: "card" }, el("h3", {}, `未来 ${H} 个交易日内的事件`),
    el("ul", { class: "reasons" }, ...v.events.map((e) => el("li", {}, `${e.date} ${e.name}`)))) : null;
  const r = s.risk;
  const risk = el("section", { class: "card" }, el("h3", {}, "风险刻画（每手）"),
    el("div", { class: "tiles" },
      tile("保证金", `约 ${num(r.margin_per_lot)} 元`, `交易所比例 ${r.margin_pct}%`),
      tile("一个涨跌停", `约 ${num(r.limit_pnl_per_lot)} 元`, `幅度 ${r.limit_pct}%`),
      tile("平均日波动 ATR14", `约 ${num(r.atr_pnl_per_lot)} 元`, `${num(r.atr14, 1)} ${s.unit}`),
      tile("20 日年化波动率", pct(r.rv_20), `近一年分位 ${pct(r.rv_pct)}`)),
    el("p", { class: "muted small" }, `期货保证金交易有杠杆；最近整数关口 ${num(s.round_number.nearest)}（距现价 ${signed(s.round_number.distance_pct)}）。`));
  const bt = v && v.backtest && v.backtest.strategy ? backtestSnippet(v.backtest, H, v.skill) : null;

  const back = el("p", {}, el("a", { href: "#/", class: "small" }, "← 返回总览"));
  const rt = el("section", { class: "card rt-card" },
    el("div", { class: "card-head" }, el("h3", {}, "实时行情"), el("span", { class: "muted small", id: "rt-status" }, "加载中…")),
    el("div", { class: "tiles", id: "rt-tiles" }),
    el("p", { class: "muted small" }, `新浪最新成交价，约 1 分钟更新，涨跌比昨结算。信号用的是 ${s.date} 日盘收盘，夜盘和之后的价格不会改变这条信号。`));
  $("view").replaceChildren(back, head, rt, verdict, expl, priceCard, contrib, probCard, ...extCards, newsCard, events, risk, bt, factorCard);
  if (state.rtLast) paintProductQuotes(state.rtLast);

  lineChart($("chart-price"), { dates, fmt: px, lines: [{ label: "主力收盘", values: slice(series.close, k) }],
    area: { label: "全部合约持仓量", short: "持仓", values: slice(series.total_oi, k) }, markers: rollIdx,
    extra: (i) => [el("div", { class: "muted small" }, `合约 ${slice(series.contract, k)[i]}`)] });
  lineChart($("chart-prob"), { dates, ref: 0.5, fmt: (x) => pct(x),
    lines: Object.keys(s.horizons).map((h) => ({ label: `${h} 日上涨概率`, values: slice(series[`prob_${h}`], k) })) });
  if ($("chart-ext")) lineChart($("chart-ext"), { dates, fmt: (x) => x.toFixed(3), lines: [{ label: "内外比价", values: slice(series.ext_ratio, k) }] });
  if ($("chart-spread")) lineChart($("chart-spread"), { dates, ref: 0, lines: spreadKeys.map(([key, label]) => ({ label, values: slice(series[key], k) })) });
  if ($("chart-net")) lineChart($("chart-net"), { dates, ref: 0, fmt: (x) => pct(x), lines: [{ label: "净多比例", values: slice(series.net_long, k) }] });
}

function tile(label, value, sub, cls = "") {
  return el("div", { class: "tile" }, el("div", { class: "tile-label" }, label), el("div", { class: `tile-value ${cls}` }, value), el("div", { class: "tile-sub" }, sub));
}
function contribBar(c) {
  if (c == null) return "—";
  const w = Math.min(Math.abs(c) / 0.4, 1) * 60;
  return el("span", { class: "cbar" }, el("span", { class: `cbar-fill ${c > 0 ? "long" : "short"}`, style: `width:${w}px;${c > 0 ? "left:60px" : `left:${60 - w}px`}` }),
    el("span", { class: "cbar-num" }, (c > 0 ? "+" : "") + c.toFixed(2)));
}
function renderText(text) {
  const out = []; let list = null;
  for (const line of text.split("\n").map((l) => l.trim()).filter(Boolean)) {
    if (/^[-*•]\s/.test(line)) { list ||= el("ul", { class: "reasons" }); list.append(el("li", {}, line.replace(/^[-*•]\s/, "").replace(/\*\*/g, ""))); }
    else { if (list) { out.push(list); list = null; } out.push(el("p", { class: "para" }, line.replace(/\*\*/g, ""))); }
  }
  if (list) out.push(list);
  return out;
}
function renderNews(news, s) {
  const d = news && news.digest;
  const items = (news && news.items) || [];
  const ai = items.filter((i) => i.stance);
  const flashes = items.filter((i) => !i.stance).slice(0, 8);
  if (!d && !items.length) return el("section", { class: "card" }, el("h3", {}, "消息面"), el("p", { class: "muted small" }, "今天还没有消息汇总（需要在批处理里配置 Claude API key）。"));
  return el("section", { class: "card" },
    el("div", { class: "card-head" }, el("h3", {}, "消息面"), d ? el("span", { class: "badge" }, `${d.date} · 情绪 ${d.sentiment > 0 ? "+" : ""}${Number(d.sentiment).toFixed(2)} · 利多 ${d.bullish} / 利空 ${d.bearish}`) : null),
    d ? el("p", { class: "para" }, d.summary) : null,
    el("p", { class: "muted small" }, "消息情绪没有足够历史可以回测，只展示、不计入打分。"),
    ai.length ? el("ul", { class: "news" }, ...ai.map((i) => el("li", {},
      el("span", { class: `stance ${i.stance === "利多" ? "long" : i.stance === "利空" ? "short" : "flat"}` }, i.stance),
      i.url ? el("a", { href: i.url, target: "_blank", rel: "noopener noreferrer" }, i.title) : el("span", {}, i.title),
      el("span", { class: "muted small" }, ` ${i.source || ""} ${(i.published_at || "").slice(0, 10)}`),
      i.reason ? el("div", { class: "muted small" }, i.reason) : null))) : null,
    flashes.length ? el("details", { class: "data-table" }, el("summary", {}, `上海金属网快讯（${flashes.length} 条）`),
      el("ul", { class: "news" }, ...flashes.map((i) => el("li", { class: "small" }, el("span", { class: "muted" }, (i.published_at || "").slice(5, 16).replace("T", " ") + " "), (i.content || i.title || "").slice(0, 160))))) : null);
}
function backtestSnippet(b, H, skill) {
  const row = (name, st) => el("tr", {}, el("td", {}, name), el("td", { class: "num" }, st.trades), el("td", { class: "num" }, pct(st.hit_rate)),
    el("td", { class: "num" }, signed(st.ann_return)), el("td", { class: "num" }, st.sharpe ?? "—"), el("td", { class: "num opt" }, signed(st.max_drawdown)));
  return el("section", { class: "card" }, el("h3", {}, `回测：每 ${H} 个交易日按信号持有一次（${b.since} 起，扣费）`),
    el("div", { class: "table-scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "策略"), el("th", { class: "num" }, "交易次数"), el("th", { class: "num" }, "胜率"), el("th", { class: "num" }, "年化"),
        el("th", { class: "num" }, "夏普"), el("th", { class: "num opt" }, "最大回撤"))),
      el("tbody", {}, row("按信号（偏多做多、偏空做空、中性空仓）", b.strategy), row("一直做多", b.always_long), row("20 日动量", b.momentum_20),
        b.gbm && b.gbm.strategy ? row("对照：梯度提升模型", b.gbm.strategy) : null))),
    el("p", { class: "muted small" }, `随机方向的胜率 95% 区间 ${pct(b.coin_hit_95[0])} ~ ${pct(b.coin_hit_95[1])}；年内 AUC ${skill?.auc_within ?? "—"}（±${skill?.auc_se ?? "—"}）。收益为简单加总的对数收益，未考虑保证金占用和复利。`));
}

// ---------- 回测页 ----------
async function renderBacktest() {
  const data = await api("/api/backtest");
  setStamp(data.meta);
  const cards = Object.entries(data.backtest).map(([h, per]) => el("section", { class: "card" },
    el("h3", {}, `持有 ${h} 个交易日（不重叠，扣手续费和 1 跳滑点）`),
    el("div", { class: "table-scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "品种"), el("th", { class: "num" }, "年内 AUC"), el("th", { class: "num opt" }, "对照：全部历史估计"),
        el("th", { class: "num" }, "信号胜率"), el("th", { class: "num" }, "信号年化"), el("th", { class: "num" }, "夏普"),
        el("th", { class: "num" }, "一直做多"), el("th", { class: "num opt" }, "20 日动量"), el("th", { class: "num opt" }, "随机胜率 95%"))),
      el("tbody", {}, ...Object.entries(per).filter(([, r]) => r.strategy).map(([code, r]) => {
        const sk = data.signals[code]?.skill?.[h] || {};
        const alt = data.expanding[`${data.signals[code]?.sector}|${h}`]?.[code] || {};
        const good = sk.auc_within != null && sk.auc_within - 0.5 >= sk.auc_se;
        return el("tr", { class: "clickable", onclick: () => { state.horizon = h; location.hash = `#/p/${code}`; } },
          el("td", {}, data.meta.products[code]?.name || code),
          el("td", { class: "num " + (good ? "up" : "") }, sk.auc_within != null ? `${sk.auc_within.toFixed(3)} ±${sk.auc_se}` : "—"),
          el("td", { class: "num opt muted" }, alt.auc_within != null ? alt.auc_within.toFixed(3) : "—"),
          el("td", { class: "num" }, pct(r.strategy.hit_rate)), el("td", { class: "num " + chgClass(r.strategy.ann_return) }, signed(r.strategy.ann_return)),
          el("td", { class: "num" }, r.strategy.sharpe ?? "—"), el("td", { class: "num " + chgClass(r.always_long.ann_return) }, signed(r.always_long.ann_return)),
          el("td", { class: "num opt " + chgClass(r.momentum_20.ann_return) }, signed(r.momentum_20.ann_return)),
          el("td", { class: "num opt muted" }, `${pct(r.coin_hit_95[0])}~${pct(r.coin_hit_95[1])}`));
      }))))));
  $("view").replaceChildren(el("section", { class: "card intro" }, el("h3", {}, "回测与可靠性"),
    el("p", { class: "muted small" }, "所有数字都来自滚动样本外预测：每个月初只用当时已知结果的数据重新估计权重，再给下一个月打分。"
      + "年内 AUC 是在每个自然年内分别衡量“打分高的日子之后是否更常上涨”再平均，0.5 等于随机猜测；"
      + "它超过 0.5 至少一个标准误才标红。对照列是用全部历史（而不是最近 3 年）估计权重的结果。"
      + "如果信号没有跑赢“一直做多”或随机方向，说明这个品种这个周期的多因子模型目前没有可靠优势。")),
    ...[forwardCard(data.forward, data.meta)].filter(Boolean), ...cards);
}

// ---------- 前瞻检验 ----------
function forwardCard(f, meta) {
  if (!f) return null;
  const hs = Object.keys(f.horizons || {});
  const aucText = (r) => r.auc == null ? "—" : `${r.auc.toFixed(3)} ±${r.auc_se}`;
  const rows = hs.flatMap((h) => {
    const res = f.horizons[h];
    const lines = ["A", "B"].map((m) => {
      const r = res[m];
      return el("tr", {}, el("td", {}, m === "A" ? `${h} 日` : ""), el("td", {}, m === "A" ? "主模型" : "对照模型"),
        el("td", { class: "num" }, `${r.matured_days} 天`), el("td", { class: "num" }, aucText(r)),
        el("td", { class: "num" }, r.active ? `${pct(r.hit_rate)}（${r.active} 次）` : "—"),
        el("td", { class: "num opt" }, pct(r.up_share)));
    });
    if (res.diff != null) lines.push(el("tr", {}, el("td", {}), el("td", { colspan: 5, class: "muted small" },
      `对照 − 主模型：AUC ${res.diff > 0 ? "+" : ""}${res.diff.toFixed(3)} ±${res.diff_se}`)));
    return lines;
  });
  const anyMatured = hs.some((h) => f.horizons[h].A.matured > 0);
  const first = f.first_results ? Object.entries(f.first_results).map(([h, d]) => `${h} 日 ${d}`).join("、") : "";
  const table = anyMatured ? el("div", { class: "table-scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, el("th", {}, "周期"), el("th", {}, "模型"), el("th", { class: "num" }, "已出结果"),
      el("th", { class: "num" }, "AUC"), el("th", { class: "num" }, "有方向时的胜率"), el("th", { class: "num opt" }, "同期上涨比例"))),
    el("tbody", {}, ...rows)))
    : el("p", { class: "muted small" }, `还没有出结果的预测。第一批结果预计在这些日子收盘后出来：${first || "—"}。`);
  let latest = null;
  if (f.latest) {
    const codes = Object.values(meta.sectors).flat().filter((c) => f.latest.probs[c]);
    latest = el("div", {},
      el("h4", {}, `${f.latest.signal_day} 两个模型的上涨概率（主模型 / 对照模型）`),
      el("div", { class: "table-scroll" }, el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "品种"), ...hs.map((h) => el("th", { class: "num" }, `${h} 日`)))),
        el("tbody", {}, ...codes.map((c) => el("tr", {}, el("td", {}, meta.products[c]?.name || c),
          ...hs.map((h) => { const v = f.latest.probs[c][h] || {}; return el("td", { class: "num" }, `${pct(v.A)} / ${pct(v.B)}`); })))))));
  }
  return el("section", { class: "card" },
    el("h3", {}, `前瞻检验：主模型 vs 对照模型（${f.start} 起，只用之后的新数据）`),
    el("p", { class: "muted small" }, `${f.models.A}；${f.models.B}。对照模型的因子是看过 2019~2026 的历史表现后挑的，`
      + `用同一段历史回测它不算数，所以只比较从 ${f.start} 起每天记下、之后不再修改的预测。`),
    el("p", { class: "muted small" }, f.rule),
    el("p", { class: "small" }, `已记录 ${f.logged_days} 个信号日，目标 ${f.target_days} 个（约一年）。样本少的时候 AUC 的标准误很大，不要提前下结论。`),
    table, latest);
}

// ---------- 数据状态 ----------
async function renderHealth() {
  const data = await api("/api/health");
  setStamp(data.meta);
  const rows = (data.sources || []).map((r) => el("tr", {}, el("td", {}, r.name), el("td", { class: "num" }, (r.last_ok || "—").replace("T", " ").slice(0, 16)),
    el("td", { class: "num" }, (r.latest || "—").replace("T", " ").slice(0, 16)), el("td", { class: "num " + (r.failures ? "up" : "") }, r.failures),
    el("td", { class: "small muted wrapcell" }, r.last_error || "")));
  const ev = (data.events || []).map((e) => el("li", {}, `${e.date} ${e.name}`));
  $("view").replaceChildren(
    el("section", { class: "card" }, el("h3", {}, "数据源状态"),
      el("p", { class: "muted small" }, data.site_db.remote ? `网页数据从私有数据集同步，上次拉取 ${(data.site_db.pulled_at || "—").replace("T", " ").slice(0, 16)}${data.site_db.pull_error ? "，失败：" + data.site_db.pull_error : ""}。` : "网页数据来自本机批处理。"),
      el("div", { class: "table-scroll" }, el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, "数据源"), el("th", { class: "num" }, "上次成功"),
        el("th", { class: "num" }, "数据最新到"), el("th", { class: "num" }, "失败次数"), el("th", {}, "最近错误"))), el("tbody", {}, ...rows)))),
    el("section", { class: "card" }, el("h3", {}, "近期事件日历"), el("p", { class: "muted small" }, "常规数据发布日为近似日期；美联储议息为官方日历。"), el("ul", { class: "reasons" }, ...ev)));
}

// ---------- 启动 ----------
(async function init() {
  try {
    const s = await api("/api/session");
    if (!s.authed) showLogin(); else route();
  } catch (err) { showError(err.message); }
})();
window.addEventListener("scroll", () => { $("tooltip").hidden = true; }, { passive: true });
