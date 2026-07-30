/* ===========================================================================================
   AGORA DASHBOARD STANDARD -- shared helper library v1
   ===========================================================================================
   SOURCE OF TRUTH: clients/_standard/dash/_lib.js
   VENDORED byte-identically into every clients/client_<c>/dash/dashboard.html, between the
   sentinel comment pair that vendor_lib.py looks for (grep AGORA STANDARD LIB in any dashboard
   to see it).

   Same posture as freshness.py / platform_sso.py: FIX IT EVERYWHERE OR NOWHERE. To change it,
   edit THIS file and run:  py -3 clients/_standard/vendor_lib.py

   TWO THINGS THIS FILE MAY NEVER CONTAIN, both of which broke the vendoring once:
     * the literal sentinel strings -- the splicer would cut inside the payload it just wrote,
       so vendor_lib.py now REFUSES to write a payload containing them,
     * a "star slash" sequence anywhere in a block comment (so no globs like clients/<star>/dash)
       -- it closes the comment early and every dashboard fails the esprima gate.

   ES5/ES2015-SAFE ONLY. No optional chaining (?.), no nullish coalescing (??): esprima 4.x --
   the pre-deploy gate tools/_validate_dash_js.py -- cannot parse them. Use classic && / ||
   guards. A syntax error here strands every dashboard on "Loading..." forever, because the
   script that fetches /data.json and swaps the DOM never runs.

   This library is deliberately BRAND-NEUTRAL: it reads CSS custom properties and never
   hardcodes a colour, a currency or a locale. Per-client brand lives in :root; per-client
   metrics live in SPEC. Nothing brand-specific belongs below this line.
   =========================================================================================== */

var NS = "http://www.w3.org/2000/svg";
var MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
var MONL = ["January","February","March","April","May","June","July","August","September","October","November","December"];
var DOW3 = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
var DOW = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];

/* Locale + currency come from the payload, so one library serves en-US / en-AU / en-NZ / de-CH.
   Defaults are only defaults -- boot() overwrites them from DATA before the first render. */
var LOC = "en-US";
var CUR = "$";

/* ===================================================================== DOM basics */
function byId(id){ return document.getElementById(id); }
function setText(id, v){ var e = byId(id); if(e){ e.textContent = v; } }
function setHTML(id, v){ var e = byId(id); if(e){ e.innerHTML = v; } }
function show(id, on){ var e = byId(id); if(e){ e.hidden = !on; } }
function esc(s){
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function el(t, a){
  var e = document.createElementNS(NS, t);
  if(a){ for(var k in a){ if(a.hasOwnProperty(k)){ e.setAttribute(k, a[k]); } } }
  return e;
}
function clear(node){ while(node && node.firstChild){ node.removeChild(node.firstChild); } }
function cssVar(name){
  var v = getComputedStyle(document.documentElement).getPropertyValue(name);
  return v ? v.trim() : "";
}

/* ===================================================================== formatters
   Numbers are ALWAYS tabular-nums in CSS so they do not jitter when they update. */
function isNum(v){ return !(v === null || v === undefined || v === "" || isNaN(Number(v))); }
function n0(v){ return isNum(v) ? Number(v) : 0; }
function num(v){ return isNum(v) ? Number(Math.round(v)).toLocaleString(LOC) : "—"; }
function money(v, d){
  if(!isNum(v)){ return "—"; }
  if(d === undefined || d === null){ d = (Math.abs(v) >= 1000 ? 0 : 2); }
  return CUR + Number(v).toLocaleString(LOC, { minimumFractionDigits:d, maximumFractionDigits:d });
}
function compact(v){
  if(!isNum(v)){ return "—"; }
  var a = Math.abs(v);
  if(a >= 1000000){ return CUR + (v/1000000).toFixed(a >= 10000000 ? 1 : 2) + "M"; }
  if(a >= 1000){ return CUR + (v/1000).toFixed(a >= 10000 ? 0 : 1) + "K"; }
  return CUR + Math.round(v);
}
function compactN(v){
  if(!isNum(v)){ return "—"; }
  var a = Math.abs(v);
  if(a >= 1000000){ return (v/1000000).toFixed(1) + "M"; }
  if(a >= 1000){ return (v/1000).toFixed(a >= 10000 ? 0 : 1) + "K"; }
  return String(Math.round(v));
}
function pct(v, d){ return isNum(v) ? Number(v).toFixed(d === undefined ? 2 : d) + "%" : "—"; }
function ratio(v, d){ return isNum(v) ? Number(v).toFixed(d === undefined ? 2 : d) + "x" : "—"; }
function safe(a, b){ return b ? a / b : 0; }
/* A field the source RETURNS BUT NEVER POPULATES must render n/a, never 0. "0" reads as
   "we got none", which is a lie. Every KPI spec can declare na:true to force this. */
function na(){ return "n/a"; }
/* Axis percent formatter that keeps enough decimals to stay distinguishable. Rounding to whole
   percent on a 1.2% axis printed "1% 1% 1% 0% 0%" -- five ticks, three identical labels. */
function pctAxis(max){
  var d = max >= 20 ? 0 : (max >= 5 ? 1 : 2);
  return function(v){ return Number(v).toFixed(d) + "%"; };
}
/* Days / hours / minutes, e.g. 2D 12H 10M -- the speed-to-lead format clients asked for. */
function fmtDHM(hours){
  if(!isNum(hours)){ return "—"; }
  var mins = Math.round(Number(hours) * 60);
  var d = Math.floor(mins / 1440); mins -= d * 1440;
  var h = Math.floor(mins / 60); mins -= h * 60;
  var out = [];
  if(d){ out.push(d + "D"); }
  if(h || d){ out.push(h + "H"); }
  out.push(mins + "M");
  return out.join(" ");
}

/* ===================================================================== dates (all UTC) */
function dObj(iso){ return new Date(String(iso).slice(0, 10) + "T00:00:00Z"); }
function isoOf(dt){ return dt.toISOString().slice(0, 10); }
function shiftDays(iso, n){ var d = dObj(iso); d.setUTCDate(d.getUTCDate() + n); return isoOf(d); }
function prettyDate(iso){
  if(!iso){ return "—"; }
  var p = String(iso).slice(0, 10).split("-");
  return MON[parseInt(p[1], 10) - 1] + " " + parseInt(p[2], 10) + ", " + p[0];
}
function monthKey(iso){ return String(iso).slice(0, 7); }
function monthLabel(key){ var p = String(key).split("-"); return MON[parseInt(p[1], 10) - 1] + " " + p[0].slice(2); }
function monthLabelLong(key){ var p = String(key).split("-"); return MONL[parseInt(p[1], 10) - 1] + " " + p[0]; }
function mday(iso){ var p = String(iso).split("-"); return parseInt(p[2], 10) + " " + MON[parseInt(p[1], 10) - 1]; }
function weekStart(iso){
  var d = dObj(iso), w = d.getUTCDay();
  d.setUTCDate(d.getUTCDate() - ((w + 6) % 7));   /* ISO weeks: Monday start */
  return isoOf(d);
}
function daysBetween(a, b){ return Math.round((dObj(b) - dObj(a)) / 86400000) + 1; }
function weeksBetween(a, b){ return Math.max(1, daysBetween(a, b) / 7); }
function clampIso(v, lo, hi){ if(v < lo){ return lo; } if(v > hi){ return hi; } return v; }
/* "Updated 20 min ago" -- relative to the viewer's clock, which is fine for a generated-at
   stamp. Anything measured FROM the data (today, days remaining) must use the pull clock. */
function agoOf(iso){
  if(!iso){ return "—"; }
  var then = new Date(iso).getTime();
  if(isNaN(then)){ return String(iso).slice(0, 16).replace("T", " "); }
  var mins = Math.round((Date.now() - then) / 60000);
  if(mins < 1){ return "just now"; }
  if(mins < 60){ return mins + " min ago"; }
  var hrs = Math.round(mins / 60);
  if(hrs < 24){ return hrs + (hrs === 1 ? " hour ago" : " hours ago"); }
  var days = Math.round(hrs / 24);
  return days + (days === 1 ? " day ago" : " days ago");
}

/* ===================================================================== time grain
   ONE rule, applied to EVERY time-series chart on every tab. Each chart owns a key in GRAIN.
   "auto" resolves from the selected span, so a 7-day range is never a single weekly dot and
   three years is never 1,100 unreadable daily points. Never hardcode a grain. */
var GRAIN = {};
function autoGrain(from, to){
  var d = daysBetween(from, to);
  if(d <= 62){ return "day"; }
  if(d <= 400){ return "week"; }
  return "month";
}
function grainOf(key, from, to){
  var g = GRAIN[key];
  if(!g || g === "auto"){ return autoGrain(from, to); }
  /* An explicit choice is STICKY, so guard it against a span it cannot carry -- step to the
     next coarser grain and let the subtitle report what was actually used. Do not silently
     reset the user's choice. */
  var days = daysBetween(from, to);
  if(g === "day" && days > 400){ g = "week"; }
  if(g === "week" && days > 2800){ g = "month"; }
  return g;
}
function bucketOf(iso, g){ return g === "day" ? String(iso).slice(0, 10) : (g === "month" ? monthKey(iso) : weekStart(iso)); }
/* Label day buckets WITH THE WEEKDAY -- "was that spike a Monday or a Saturday" is usually
   the actual question. Full name in tooltips. */
function dayLabel(iso){
  var p = String(iso).split("-");
  return DOW3[dObj(iso).getUTCDay()] + " " + parseInt(p[2], 10) + " " + MON[parseInt(p[1], 10) - 1];
}
function dayFull(iso){
  var p = String(iso).split("-");
  return DOW[dObj(iso).getUTCDay()] + ", " + MON[parseInt(p[1], 10) - 1] + " " + parseInt(p[2], 10) + " " + p[0];
}
function bucketLabel(k, g){ return g === "month" ? monthLabel(k) : (g === "day" ? dayLabel(k) : mday(k)); }
function bucketTip(k, g){ return g === "month" ? monthLabelLong(k) : (g === "day" ? dayFull(k) : "Week of " + prettyDate(k)); }
/* EVERY bucket in the range, including empties -- a day with no orders must plot as a ZERO,
   not vanish, or a 7-day week renders 6 points and the quiet day silently disappears, which
   on a weekly review is the opposite of useful. Capped so Day over years cannot explode. */
function denseBuckets(from, to, g, cap){
  var out = [], seen = {}, guard = 0, lim = cap || 420, d = from;
  while(d <= to && guard++ < 6000){
    var k = bucketOf(d, g);
    if(!seen[k]){ seen[k] = 1; out.push(k); }
    d = shiftDays(d, 1);
  }
  return out.length > lim ? null : out;
}
function grainNote(g, n){
  var word = g === "day" ? "day" : (g === "month" ? "month" : "week");
  return n + " " + word + (n === 1 ? "" : "s");
}

/* ===================================================================== date presets
   Relative presets anchor to the LATEST DATE IN THE DATA, never to today. Feeds lag, and
   different feeds lag differently: "last 30 days" against today on a lagging feed renders an
   empty dashboard and looks broken. */
function presetRange(days, maxIso, minIso){
  if(days === "all"){ return [minIso, maxIso]; }
  if(days === "ytd"){
    var y0 = maxIso.slice(0, 4) + "-01-01";
    return [y0 < minIso ? minIso : y0, maxIso];
  }
  if(days === "lastyear"){
    /* The whole PREVIOUS CALENDAR YEAR is the default benchmark: a rolling 12 months
       over-weights whichever season it starts in. */
    var y = parseInt(maxIso.slice(0, 4), 10) - 1;
    var f = y + "-01-01", t = y + "-12-31";
    if(f < minIso){ f = minIso; }
    if(t > maxIso){ t = maxIso; }
    if(f > t){ return presetRange("365", maxIso, minIso); }
    return [f, t];
  }
  if(days === "prevweek"){
    var ws = completeWeek(maxIso, 2);
    return [ws[0], ws[1]];
  }
  var ff = shiftDays(maxIso, -(parseInt(days, 10) - 1));
  if(ff < minIso){ ff = minIso; }
  return [ff, maxIso];
}
/* The Nth most recent COMPLETE Mon-Sun week the data actually covers. n=1 is last week.
   Snapping to complete weeks is what stops a partial current week sneaking into a report. */
function completeWeek(maxIso, n){
  var thisWeek = weekStart(maxIso);
  var lastComplete = shiftDays(thisWeek, -1);          /* the Sunday before this week */
  var end = shiftDays(lastComplete, -7 * (n - 1));
  var start = weekStart(end);
  return [start, end];
}

/* ===================================================================== tooltip */
var _tipEl = null;
function showTip(html, x, y){
  if(!_tipEl){ _tipEl = byId("tip"); }
  if(!_tipEl){ return; }
  _tipEl.innerHTML = html;
  _tipEl.style.opacity = "1";
  var w = _tipEl.offsetWidth, h = _tipEl.offsetHeight;
  var lx = x + 14, ly = y - h - 12;
  if(lx + w > window.innerWidth - 10){ lx = x - w - 14; }
  if(lx < 8){ lx = 8; }
  if(ly < 8){ ly = y + 18; }
  _tipEl.style.left = lx + "px";
  _tipEl.style.top = ly + "px";
}
function hideTip(){ if(_tipEl){ _tipEl.style.opacity = "0"; } }
function tipTitle(t){ return '<div class="td">' + esc(t) + "</div>"; }
function tipRow(m, v){ return '<div class="tr"><span class="m">' + esc(m) + "</span><span>" + v + "</span></div>"; }
function tipNote(t){ return '<div class="tn">' + esc(t) + "</div>"; }
function bindTip(node, html){
  node.addEventListener("mousemove", function(e){ showTip(html, e.clientX, e.clientY); });
  node.addEventListener("mouseleave", hideTip);
}

/* ===================================================================== toast */
var _toastT = null;
function toast(msg){
  var t = byId("toast");
  if(!t){ return; }
  t.textContent = msg;
  t.classList.add("on");
  if(_toastT){ clearTimeout(_toastT); }
  _toastT = setTimeout(function(){ t.classList.remove("on"); }, 2600);
}

/* ===================================================================== chart frame */
function niceMax(v){
  if(!(v > 0)){ return 1; }
  var e = Math.pow(10, Math.floor(Math.log(v) / Math.LN10));
  var f = v / e;
  var n = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return n * e;
}
function ensureDefs(svg){
  var d = svg.querySelector("defs");
  if(!d){ d = el("defs"); svg.insertBefore(d, svg.firstChild); }
  return d;
}
/* Shading NEVER encodes anything -- it is always the same hue as the mark it belongs to, so it
   cannot be misread as a category. Gradient ids are per-svg + per-colour, so two charts on one
   page never collide. */
var _gradN = 0;
function areaGrad(svg, color, top, bottom){
  var id = "ag" + (++_gradN);
  var g = el("linearGradient", { id:id, x1:"0", y1:"0", x2:"0", y2:"1" });
  var a = el("stop", { offset:"0%", "stop-color":color, "stop-opacity":String(top === undefined ? 0.28 : top) });
  var b = el("stop", { offset:"100%", "stop-color":color, "stop-opacity":String(bottom === undefined ? 0.02 : bottom) });
  g.appendChild(a); g.appendChild(b);
  ensureDefs(svg).appendChild(g);
  return "url(#" + id + ")";
}
/* Light hairline gridlines on the VALUE axis only. No chart borders, no 3D, no gradients on
   data marks. Axis units go on the axis, not in a legend. */
function gridY(svg, box, ymax, fmt, ticks){
  var t = ticks || 4, i;
  for(i = 0; i <= t; i++){
    var v = ymax * (i / t);
    var y = box.y + box.h - (v / ymax) * box.h;
    svg.appendChild(el("line", { class:"gl", x1:box.x, y1:y.toFixed(1), x2:box.x + box.w, y2:y.toFixed(1) }));
    var lb = el("text", { class:"axis", x:box.x - 7, y:(y + 3.5).toFixed(1), "text-anchor":"end" });
    lb.textContent = fmt ? fmt(v) : compactN(v);
    svg.appendChild(lb);
  }
}
/* Bar charts need room: clip to the most recent N buckets and SAY SO, rather than drawing
   1,100 slivers. Returns {keys, clipped}. */
function clipBuckets(keys, maxBars){
  var lim = maxBars || 40;
  if(keys.length <= lim){ return { keys:keys, clipped:0 }; }
  return { keys:keys.slice(keys.length - lim), clipped:keys.length - lim };
}
/* One invisible hover band per index, rather than per-point hit targets. */
function hoverBands(svg, box, keys, htmlOf){
  var bw = box.w / Math.max(1, keys.length), i;
  for(i = 0; i < keys.length; i++){
    (function(idx){
      var r = el("rect", { x:(box.x + idx * bw).toFixed(1), y:box.y, width:bw.toFixed(1), height:box.h, fill:"transparent" });
      bindTip(r, htmlOf(idx));
      svg.appendChild(r);
    })(i);
  }
}

/* ===================================================================== repeated blocks
   The three blocks that appear on every dashboard, built once. */

/* A KPI tile. spec: {label, value, delta, note, accent, sel, on, key, na}
   When sel is true the tile IS a series toggle: its accent bar and dot ARE the line colour on
   the chart below, so no legend lookup is needed. */
function kpiTile(spec){
  var cls = "kpi" + (spec.sel ? " sel" : "") + (spec.sel && !spec.on ? " off" : "") + (spec.hero ? " hero" : "") + (spec.na ? " na" : "");
  var accent = spec.accent || "var(--brand)";
  var dot = spec.sel ? '<span class="dot" style="background:' + accent + '"></span>' : "";
  var d = "";
  if(spec.delta !== undefined && spec.delta !== null && spec.delta !== ""){ d = " " + spec.delta; }
  return '<div class="' + cls + '"' + (spec.key ? ' data-kpi="' + esc(spec.key) + '"' : "") +
         ' style="--accent:' + accent + '"' + (spec.title ? ' title="' + esc(spec.title) + '"' : "") + ">" +
         '<div class="kl">' + dot + esc(spec.label) + "</div>" +
         '<div class="kv">' + spec.value + "</div>" +
         '<div class="kd">' + (spec.note ? esc(spec.note) : "") + d + "</div>" +
         "</div>";
}
/* A signed delta span. good tells us which DIRECTION is good, so a cost metric goes green when
   it falls: deltaSpan(cpl, benchCpl, "down"). Green ALWAYS means better. */
function deltaSpan(cur, base, goodDir, fmt){
  if(!isNum(cur) || !isNum(base) || !base){ return ""; }
  var change = (cur - base) / Math.abs(base) * 100;
  var flat = Math.abs(change) < 0.5;
  var better = goodDir === "down" ? change < 0 : change > 0;
  var cls = flat ? "flat" : (better ? "up" : "dn");
  var arrow = flat ? "→" : (change > 0 ? "▲" : "▼");
  var txt = fmt ? fmt(Math.abs(change)) : (Math.abs(change).toFixed(0) + "%");
  return '<span class="delta ' + cls + '">' + arrow + " " + txt + "</span>";
}
/* Period-vs-benchmark compare card.
   rows: [{label, cur, bench, fmt, goodDir, basis}]
   The two columns mean DIFFERENT things by metric kind, and the card says so:
     VOLUME metrics  -> the benchmark column is the AVERAGE PER WEEK over the benchmark range,
                        and the arrow compares the period total against what that weekly rate
                        would have produced over the same number of weeks (like-for-like
                        whatever period is picked).
     RATE/COST metrics -> the AGGREGATE RATIO across the whole benchmark, never the average of
                        daily ratios, which skews badly when spend is uneven. */
function cmpCard(title, tag, rows){
  var h = '<div class="cmp-head"><h3>' + esc(title) + "</h3>" +
          (tag ? '<span class="tag">' + esc(tag) + "</span>" : "") + "</div>" +
          '<div class="cmp-cols"><span>Selected period</span><span class="b">KPI benchmark</span></div>';
  var i;
  for(i = 0; i < rows.length; i++){
    var r = rows[i];
    var f = r.fmt || num;
    h += '<div class="cmp-row">' +
           '<div class="cell"><div class="l">' + esc(r.label) + "</div>" +
             '<div class="v">' + f(r.cur) + deltaSpan(r.cur, r.bench, r.goodDir || "up") + "</div></div>" +
           '<div class="cell bench"><div class="l">' + esc(r.basis || "benchmark") + "</div>" +
             '<div class="v">' + f(r.bench) + "</div></div>" +
         "</div>";
  }
  return h;
}
/* A funnel stage card, framed as CARRY-THROUGH, not loss: "38.2% carried through" rather than
   "61.8% dropped". Same arithmetic, actionable framing. Do not revert it. */
function stageCard(i, title, note, rows){
  var h = '<div class="stage s' + ((i % 4) + 1) + '">' +
          '<div class="stage-head"><h3>' + esc(title) + "</h3>" +
          '<div class="n">' + esc(note || "") + "</div></div>" +
          '<div class="stage-body">';
  var j;
  for(j = 0; j < rows.length; j++){
    var r = rows[j];
    var f = r.fmt || num;
    h += '<div class="cmp-row">' +
           '<div class="cell"><div class="l">' + esc(r.label) + "</div>" +
             '<div class="v">' + f(r.cur) + deltaSpan(r.cur, r.bench, r.goodDir || "up") + "</div></div>" +
           '<div class="cell bench"><div class="l">' + esc(r.basis || "benchmark") + "</div>" +
             '<div class="v">' + f(r.bench) + "</div></div>" +
         "</div>";
  }
  return h + "</div></div>";
}
/* The insight strip. This is the single highest-value thing on the page: it turns a report into
   an opinion, and it is what gets quoted back to you in meetings. Every tab ends with one, and
   every card RECOMPUTES with the filters. */
function insightCard(icon, title, body, tone){
  return '<div class="insight' + (tone ? " " + tone : "") + '">' +
         '<div class="ico" aria-hidden="true">' + icon + "</div>" +
         "<div><div class=\"it\">" + esc(title) + '</div><div class="ib">' + body + "</div></div>" +
         "</div>";
}
/* A wired-looking empty state. NEVER a blank space and never an error: say what the panel is
   waiting for and what enables it. */
function emptyState(title, body){
  return '<div class="empty"><h4>' + esc(title) + "</h4><p>" + body + "</p></div>";
}
/* A status pill that carries a GLYPH AND A WORD, so colour is reinforcement and never the only
   signal -- the board survives greyscale printing and colour-blind readers. */
function lamp(kind, word, glyph){
  return '<span class="lamp ' + esc(kind) + '"><span class="b" aria-hidden="true">' + (glyph || "●") +
         "</span>" + esc(word) + "</span>";
}
/* The five-second read: colour + glyph + one plain-English sentence, above the fold, no
   interaction required. */
function verdictBlock(tone, glyph, headline, sub, counts){
  var h = '<div class="verdict ' + esc(tone) + '">' +
          '<div class="vico" aria-hidden="true"><span>' + glyph + "</span></div>" +
          '<div class="vtxt"><div class="vh1">' + esc(headline) + "</div>" +
          '<div class="vs">' + sub + "</div>";
  if(counts && counts.length){
    h += '<div class="vcounts">';
    var i;
    for(i = 0; i < counts.length; i++){
      h += '<span class="vcount"><span class="n">' + counts[i][1] + "</span>" + esc(counts[i][0]) + "</span>";
    }
    h += "</div>";
  }
  return h + "</div></div>";
}
/* Cap long tables and SAY SO. "Top 10 of 55" with a footer confirming the totals cover all 55,
   so a capped table can never be mistaken for the whole set. */
function capNote(shown, total, what, extra){
  if(total <= shown){ return total + " " + what + (extra ? " · " + extra : ""); }
  return "Top " + shown + " of " + total + " " + what + " · totals below cover all " + total +
         (extra ? " · " + extra : "");
}

/* ===================================================================== controls */

/* One helper wires EVERY segmented control on the page. A new toggle is one call. */
function segWire(id, apply){
  var box = byId(id);
  if(!box){ return; }
  var btns = box.querySelectorAll("button");
  for(var i = 0; i < btns.length; i++){
    (function(b){
      b.addEventListener("click", function(){
        for(var j = 0; j < btns.length; j++){ btns[j].classList.remove("on"); btns[j].setAttribute("aria-pressed", "false"); }
        b.classList.add("on"); b.setAttribute("aria-pressed", "true");
        apply(b.getAttribute("data-v"));
        renderAll();
      });
    })(btns[i]);
  }
}
function setActive(sel, node){
  var n = document.querySelectorAll(sel);
  for(var i = 0; i < n.length; i++){ n[i].classList.remove("active"); }
  if(node){ node.classList.add("active"); }
}
/* Debounce a free-text filter (~220 ms), or every keystroke re-renders the page. */
function debounce(fn, ms){
  var t = null;
  return function(){
    var args = arguments, self = this;
    if(t){ clearTimeout(t); }
    t = setTimeout(function(){ fn.apply(self, args); }, ms || 220);
  };
}

/* EVERY column header is a sort control. A <table> declares its state key with data-sort and
   each sortable column with data-k on its <th>; this walks them all, so a new table is
   sortable the moment it is added, with no extra wiring. */
var SORT = {};
function wireSorts(){
  var tables = document.querySelectorAll("table[data-sort]");
  for(var t = 0; t < tables.length; t++){
    (function(tbl){
      var stKey = tbl.getAttribute("data-sort");
      if(!SORT[stKey]){ SORT[stKey] = { k:"", dir:-1 }; }
      var th = tbl.querySelectorAll("thead th[data-k]");
      for(var i = 0; i < th.length; i++){
        (function(h){
          if(h.className.indexOf("srt") < 0){ h.className = (h.className + " srt").trim(); }
          if(h.innerHTML.indexOf('class="ar"') < 0){ h.innerHTML = h.innerHTML + '<span class="ar">↕</span>'; }
          h.setAttribute("tabindex", "0");
          h.setAttribute("role", "columnheader");
          var go = function(){
            var k = h.getAttribute("data-k");
            var s = SORT[stKey];
            /* First click on a new column sorts DESC (biggest first, which is what people
               want); clicking the active column flips it. */
            if(s.k === k){ s.dir = -s.dir; } else { s.k = k; s.dir = -1; }
            renderAll();
          };
          h.addEventListener("click", go);
          h.addEventListener("keydown", function(e){ if(e.key === "Enter" || e.key === " "){ e.preventDefault(); go(); } });
        })(th[i]);
      }
    })(tables[t]);
  }
}
function markSort(tblId, st){
  var th = document.querySelectorAll("#" + tblId + " thead th[data-k]");
  for(var i = 0; i < th.length; i++){
    var on = th[i].getAttribute("data-k") === st.k;
    th[i].className = th[i].className.replace(/\s*\bon\b/g, "") + (on ? " on" : "");
    th[i].setAttribute("aria-sort", on ? (st.dir < 0 ? "descending" : "ascending") : "none");
    var ar = th[i].querySelector(".ar");
    if(ar){ ar.textContent = on ? (st.dir < 0 ? "↓" : "↑") : "↕"; }
  }
}
function sortRows(rows, st){
  var k = st.k, dir = st.dir;
  if(!k){ return rows.slice(); }
  return rows.slice().sort(function(a, b){
    var x = a[k], y = b[k];
    if(typeof x === "string" || typeof y === "string"){
      x = String(x === undefined || x === null ? "" : x).toLowerCase();
      y = String(y === undefined || y === null ? "" : y).toLowerCase();
      return x < y ? -dir : (x > y ? dir : 0);
    }
    return ((x || 0) - (y || 0)) * dir;
  });
}

/* Tab state lives in the URL HASH: linkable, and it survives a refresh. */
function wireTabs(){
  var tabs = document.querySelectorAll(".tab");
  for(var i = 0; i < tabs.length; i++){
    (function(t){ t.addEventListener("click", function(){ setTab(t.getAttribute("data-tab")); }); })(tabs[i]);
  }
  window.addEventListener("hashchange", function(){
    var h = location.hash.replace("#", "");
    if(h && h !== currentTab()){ setTab(h, true); }
  });
}
function setTab(name, keepScroll){
  var tabs = document.querySelectorAll(".tab"), i, found = false;
  for(i = 0; i < tabs.length; i++){
    var on = tabs[i].getAttribute("data-tab") === name;
    if(on){ found = true; }
    tabs[i].setAttribute("aria-selected", on ? "true" : "false");
  }
  if(!found){ return; }
  var panes = document.querySelectorAll(".tab-pane");
  for(i = 0; i < panes.length; i++){ panes[i].hidden = panes[i].id !== ("tab-" + name); }
  if(location.hash !== "#" + name){
    try { history.replaceState(null, "", "#" + name); } catch(e){ location.hash = name; }
  }
  if(!keepScroll){ window.scrollTo({ top:0, behavior:"smooth" }); }
  renderAll();
}
function currentTab(){
  var t = document.querySelector('.tab[aria-selected="true"]');
  return t ? t.getAttribute("data-tab") : "";
}
function initialTab(fallback){
  var h = location.hash.replace("#", "");
  if(h && document.querySelector('.tab[data-tab="' + h + '"]')){ return h; }
  return fallback;
}

/* The filter bar is TUCKABLE, because filters should not own the screen. Collapsed it keeps the
   one-line summary of what is selected, the choice persists in localStorage, and ONE toggle
   collapses every tab's bar. Ours went 130px -> 33px. */
var TUCK_KEY = "agora_dash_tuck";
function isTucked(){
  try { return window.localStorage.getItem(TUCK_KEY) === "1"; } catch(e){ return false; }
}
function setTuck(on){
  var cs = document.querySelectorAll(".controls"), i;
  for(i = 0; i < cs.length; i++){
    if(on){ cs[i].classList.add("tucked"); } else { cs[i].classList.remove("tucked"); }
  }
  var ls = document.querySelectorAll(".ctuck .cl");
  for(i = 0; i < ls.length; i++){ ls[i].textContent = on ? "Filters" : "Hide"; }
  try { window.localStorage.setItem(TUCK_KEY, on ? "1" : "0"); } catch(e){ /* private mode */ }
}
function wireTuck(){
  var bs = document.querySelectorAll(".ctuck"), i;
  for(i = 0; i < bs.length; i++){
    bs[i].addEventListener("click", function(){
      setTuck(!document.querySelector(".controls").classList.contains("tucked"));
    });
  }
  setTuck(isTucked());
}

/* Cross-filter chips. Any visual element representing a category can filter the tab; clicking
   the SAME element again clears it, the active filter shows as a chip with an x, and the
   SOURCE chart stays unfiltered so the user can see what they picked out of. */
function chipRow(chips){
  if(!chips.length){ return ""; }
  var h = "", i;
  for(i = 0; i < chips.length; i++){
    h += '<span class="chip">' + esc(chips[i].label) +
         '<button type="button" data-clear="' + esc(chips[i].key) + '" aria-label="Clear ' + esc(chips[i].label) + '">✕</button></span>';
  }
  return h;
}
function wireChips(hostId, onClear){
  var host = byId(hostId);
  if(!host){ return; }
  host.addEventListener("click", function(e){
    var b = e.target;
    if(b && b.getAttribute && b.getAttribute("data-clear")){ onClear(b.getAttribute("data-clear")); }
  });
}

/* ===================================================================== freshness + sync
   A dashboard that cannot tell you it is stale is worse than none. Show WHEN the data was
   generated and WHAT IT COVERS, and turn the latter red past a threshold. */
var STALE_DAYS = 3;
function renderFreshness(data){
  setText("updated", agoOf(data.last_updated));
  var thru = byId("thru");
  if(thru){
    var t = data.data_through ? String(data.data_through).slice(0, 10) : "";
    thru.textContent = t ? prettyDate(t) : "—";
    var lag = t ? Math.round((Date.now() - dObj(t).getTime()) / 86400000) : 999;
    if(lag > STALE_DAYS){ thru.classList.add("stale"); thru.title = lag + " days behind"; }
    else { thru.classList.remove("stale"); thru.title = ""; }
  }
}
/* Sync: trigger the export job if it can, else re-fetch the payload. ALWAYS show a spinner and
   update the timestamp. The route carries a cooldown keyed on the data object's AGE (so it is
   shared across instances) -- that is what stops repeat clicks running up paid API calls.
   A dashboard with no /refresh route degrades to a re-fetch instead of a dead button. */
var SYNCING = false;
function doSync(){
  if(SYNCING){ return; }
  var btn = byId("syncBtn"), lbl = byId("syncLbl");
  SYNCING = true;
  if(btn){ btn.classList.add("busy"); btn.classList.remove("ok", "bad"); btn.disabled = true; }
  if(lbl){ lbl.textContent = "Syncing"; }
  fetch("/refresh", { method:"POST", credentials:"same-origin" })
    .then(function(r){
      if(r.status === 404 || r.status === 405){ return { fallback:true }; }
      return r.json().catch(function(){ return { ok:r.ok }; });
    })
    .catch(function(){ return { fallback:true }; })
    .then(function(res){
      var soft = res && (res.fallback || res.ok === false);
      return loadData().then(function(){
        if(btn){ btn.classList.remove("busy"); btn.classList.add(soft ? "ok" : "ok"); btn.disabled = false; }
        if(lbl){ lbl.textContent = "Synced"; }
        toast(soft ? "Reloaded the latest published data." : "Sync complete.");
        setTimeout(function(){ if(lbl){ lbl.textContent = "Sync"; } if(btn){ btn.classList.remove("ok"); } }, 4000);
        SYNCING = false;
      });
    })
    .catch(function(err){
      if(btn){ btn.classList.remove("busy"); btn.classList.add("bad"); btn.disabled = false; }
      if(lbl){ lbl.textContent = "Failed"; }
      toast("Could not sync: " + (err && err.message ? err.message : "unknown error"));
      setTimeout(function(){ if(lbl){ lbl.textContent = "Sync"; } if(btn){ btn.classList.remove("bad"); } }, 5000);
      SYNCING = false;
    });
}

/* ===================================================================== boot
   The page shows #boot until the fetch resolves, then swaps in #app. A JS error before this
   point strands the page on the loading state forever -- which is exactly why the esprima gate
   exists. */
var DATA = null;
function loadData(){
  return fetch("/data.json", { credentials:"same-origin" })
    .then(function(r){
      if(!r.ok){ throw new Error("HTTP " + r.status); }
      return r.json();
    })
    .then(function(d){
      DATA = d;
      if(d.locale){ LOC = d.locale; }
      if(d.currency){ CUR = d.currency; }
      applyBrand(d.brand);
      renderFreshness(d);
      onData(d);
      show("boot", false);
      show("app", true);
      renderAll();
    });
}
function bootFail(msg){
  var b = byId("boot");
  if(!b){ return; }
  b.className = "loading";
  b.innerHTML = '<div class="err"><b>Could not load the dashboard data.</b>' + esc(msg) +
                "<br>The page itself is fine — this is the data fetch failing. Try Sync, or check the export job.</div>";
}
/* Logos are inlined data-URIs, never a CDN fetch: the deploy is self-contained and the bucket
   stays private. An <img> that never loads must not render its alt text into the header. */
function applyBrand(brand){
  if(!brand){ return; }
  var l = byId("logo");
  if(l && brand.logo){ l.src = brand.logo; l.alt = brand.name || ""; l.hidden = false; }
  var a = byId("agora");
  if(a && brand.agora_logo){ a.src = brand.agora_logo; a.hidden = false; }
  if(brand.name){ setText("wm", brand.name); }
  if(brand.tagline){ setText("tg", brand.tagline); }
  if(brand.favicon){ var f = byId("favicon"); if(f){ f.href = brand.favicon; } }
}
function boot(fallbackTab){
  wireTabs();
  wireTuck();
  wireSorts();
  var s = byId("syncBtn");
  if(s){ s.addEventListener("click", doSync); }
  document.addEventListener("keydown", function(e){ if(e.key === "Escape"){ closeModal(); } });
  loadData()
    .then(function(){ setTab(initialTab(fallbackTab), true); })
    .catch(function(err){ bootFail(err && err.message ? err.message : "unknown error"); });
}

/* ===========================================================================================
   CHART PRIMITIVES
   ===========================================================================================
   Hand-rolled inline SVG, no CDN: the deploy is self-contained and the esprima gate parses the
   JS that draws it. Every primitive takes its colours from the caller (which takes them from
   --s1..--s8), so nothing here knows a brand.
   =========================================================================================== */

/* Multi-series time chart. Bars for the first series when opts.bars is set, lines for the rest.
   series: [{key, label, color, fmt, axis:"count"|"money"|"rate", dash, on}]
   mode: "abs" -> real values on a DEDICATED AXIS PER FAMILY (counts left, money right, rates
                  a third), because on a totals axis a ~$230 AOV is a flat line under a $5K peak.
                  Which axis a line belongs to is shown by its DASH PATTERN.
         "rel" -> index every series to ITS OWN PEAK on a shared 0-100% axis, so metrics on
                  wildly different scales compare BY SHAPE. Tooltips still show real values
                  plus "(N% of peak)". */
function drawSeries(svgId, keys, rows, series, opts){
  var svg = byId(svgId);
  if(!svg){ return; }
  clear(svg);
  opts = opts || {};
  var vb = (svg.getAttribute("viewBox") || "0 0 1180 260").split(/\s+/);
  var W = Number(vb[2]), H = Number(vb[3]);
  var box = { x:opts.padL || 54, y:14, w:W - (opts.padL || 54) - (opts.padR || 54), h:H - 14 - 30 };
  var on = [], i, s;
  for(i = 0; i < series.length; i++){ if(series[i].on !== false){ on.push(series[i]); } }
  if(!keys.length || !on.length){
    var t = el("text", { class:"axis", x:W / 2, y:H / 2, "text-anchor":"middle" });
    t.textContent = keys.length ? "Select a scorecard to plot it." : "No data in the selected period.";
    svg.appendChild(t);
    return;
  }
  var rel = opts.mode === "rel";
  /* peaks per series, used by both modes */
  var peak = {};
  for(s = 0; s < on.length; s++){
    var mx = 0;
    for(i = 0; i < keys.length; i++){
      var v = n0(rows[keys[i]] ? rows[keys[i]][on[s].key] : 0);
      if(v > mx){ mx = v; }
    }
    peak[on[s].key] = mx;
  }
  /* one max per axis family in absolute mode, a flat 0-100 in relative mode */
  var fam = { count:0, money:0, rate:0 };
  for(s = 0; s < on.length; s++){
    var f = on[s].axis || "count";
    if(peak[on[s].key] > fam[f]){ fam[f] = peak[on[s].key]; }
  }
  for(var k in fam){ if(fam.hasOwnProperty(k)){ fam[k] = niceMax(fam[k]); } }
  var yMax = rel ? 100 : null;
  function yOf(ser, v){
    var top = rel ? 100 : fam[ser.axis || "count"];
    var val = rel ? (peak[ser.key] ? (n0(v) / peak[ser.key]) * 100 : 0) : n0(v);
    return box.y + box.h - (val / (top || 1)) * box.h;
  }
  /* the LEFT axis: 0-100% in relative mode, else the busiest family */
  var leftFam = "count";
  var best = -1;
  for(s = 0; s < on.length; s++){
    var ff = on[s].axis || "count";
    if(fam[ff] > best){ best = fam[ff]; leftFam = ff; }
  }
  gridY(svg, box, rel ? 100 : fam[leftFam], rel ? function(v){ return Math.round(v) + "%"; } :
        (leftFam === "money" ? compact : (leftFam === "rate" ? pctAxis(fam.rate) : compactN)));

  /* x labels: never more than ~9, or they collide */
  var step = Math.max(1, Math.ceil(keys.length / 9));
  var bw = box.w / keys.length;
  for(i = 0; i < keys.length; i += step){
    var lx = box.x + i * bw + bw / 2;
    var lb = el("text", { class:"axis", x:lx.toFixed(1), y:H - 10, "text-anchor":"middle" });
    lb.textContent = bucketLabel(keys[i], opts.grain || "day");
    svg.appendChild(lb);
  }
  /* bars first (behind), then lines */
  for(s = 0; s < on.length; s++){
    if(!(opts.bars && s === 0)){ continue; }
    for(i = 0; i < keys.length; i++){
      var bv = n0(rows[keys[i]] ? rows[keys[i]][on[s].key] : 0);
      var by = yOf(on[s], bv), bh = Math.max(0, box.y + box.h - by);
      svg.appendChild(el("rect", { x:(box.x + i * bw + bw * 0.18).toFixed(1), y:by.toFixed(1),
        width:Math.max(1, bw * 0.64).toFixed(1), height:bh.toFixed(1), fill:on[s].color, opacity:"0.55", rx:"2" }));
    }
  }
  var DASH = { count:"", money:"7 4", rate:"2 3" };
  for(s = 0; s < on.length; s++){
    if(opts.bars && s === 0){ continue; }
    var pts = [], i2;
    for(i2 = 0; i2 < keys.length; i2++){
      var vv = rows[keys[i2]] ? rows[keys[i2]][on[s].key] : 0;
      pts.push((box.x + i2 * bw + bw / 2).toFixed(1) + "," + yOf(on[s], vv).toFixed(1));
    }
    var attrs = { points:pts.join(" "), fill:"none", stroke:on[s].color, "stroke-width":"2.2",
                  "stroke-linejoin":"round", "stroke-linecap":"round" };
    if(!rel && DASH[on[s].axis || "count"]){ attrs["stroke-dasharray"] = DASH[on[s].axis || "count"]; }
    svg.appendChild(el("polyline", attrs));
  }
  /* ONE invisible hover band per index, rather than per-point hit targets */
  hoverBands(svg, box, keys, function(idx){
    var r = rows[keys[idx]] || {};
    var h = tipTitle(bucketTip(keys[idx], opts.grain || "day")), j;
    for(j = 0; j < on.length; j++){
      var ser = on[j], raw = r[ser.key];
      var txt = (ser.fmt || num)(raw);
      if(rel && peak[ser.key]){ txt += " (" + Math.round(n0(raw) / peak[ser.key] * 100) + "% of peak)"; }
      h += tipRow(ser.label, txt);
    }
    return h;
  });
  var note = byId(svgId + "Note");
  if(note){
    note.innerHTML = rel
      ? "<b>Relative</b> indexes every series to its own peak on a shared 0–100% axis, so metrics on different scales compare by shape. Tooltips carry the real values."
      : "<b>Absolute</b> plots real values, one axis per family — counts solid, money long-dash, rates dotted.";
  }
}

/* A funnel. Stage widths use a SQRT scale, or the tail vanishes next to impressions.
   stages: [{label, value, fmt, color}] -- each row shows the carry-through from the one above,
   framed as a conversion, not a loss. */
function drawFunnel(hostId, stages){
  var host = byId(hostId);
  if(!host){ return; }
  if(!stages.length){ host.innerHTML = emptyState("Nothing to funnel yet", "No rows in the selected period."); return; }
  var top = 0, i;
  for(i = 0; i < stages.length; i++){ if(n0(stages[i].value) > top){ top = n0(stages[i].value); } }
  if(!top){ top = 1; }
  var h = "";
  for(i = 0; i < stages.length; i++){
    var st = stages[i];
    var w = Math.sqrt(n0(st.value) / top) * 100;
    var prev = i ? n0(stages[i - 1].value) : 0;
    var carry = i && prev ? (n0(st.value) / prev * 100) : null;
    h += '<div class="frow">' +
           '<div class="fl">' + esc(st.label) + "</div>" +
           '<div class="fbar"><i style="width:' + Math.max(0.6, w).toFixed(1) + "%;background:" + (st.color || "var(--s1)") + '"></i></div>' +
           '<div class="fv"><b>' + (st.fmt || num)(st.value) + "</b>" +
             (carry === null ? "" : ' <span class="cv">' + carry.toFixed(carry < 10 ? 2 : 1) + "% carried through</span>") +
           "</div></div>";
  }
  host.innerHTML = h;
}

/* Horizontal ranked bars. Cheapest/biggest at the top; the bar is the gap to the best row.
   rows: [{label, value, note, color}] */
function drawRanked(svgId, rows, fmt, opts){
  var svg = byId(svgId);
  if(!svg){ return; }
  clear(svg);
  opts = opts || {};
  var vb = (svg.getAttribute("viewBox") || "0 0 420 380").split(/\s+/);
  var W = Number(vb[2]), H = Number(vb[3]);
  if(!rows.length){
    var t = el("text", { class:"axis", x:W / 2, y:H / 2, "text-anchor":"middle" });
    t.textContent = "No segments above the volume floor.";
    svg.appendChild(t);
    return;
  }
  var labelW = opts.labelW || 96;
  var mx = 0, i;
  for(i = 0; i < rows.length; i++){ if(n0(rows[i].value) > mx){ mx = n0(rows[i].value); } }
  mx = niceMax(mx);
  var rowH = Math.min(34, (H - 8) / rows.length), bw = W - labelW - 62;
  for(i = 0; i < rows.length; i++){
    var y = 4 + i * rowH;
    var lb = el("text", { class:"axis", x:labelW - 8, y:(y + rowH / 2 + 3.5).toFixed(1), "text-anchor":"end" });
    lb.textContent = rows[i].label;
    svg.appendChild(lb);
    var w = (n0(rows[i].value) / mx) * bw;
    var r = el("rect", { x:labelW, y:(y + rowH * 0.18).toFixed(1), width:Math.max(1, w).toFixed(1),
      height:(rowH * 0.64).toFixed(1), fill:rows[i].color || "var(--s1)", rx:"3" });
    bindTip(r, tipTitle(rows[i].label) + tipRow(opts.metric || "Value", (fmt || num)(rows[i].value)) +
      (rows[i].note ? tipNote(rows[i].note) : ""));
    svg.appendChild(r);
    var vt = el("text", { class:"axis", x:(labelW + w + 6).toFixed(1), y:(y + rowH / 2 + 3.5).toFixed(1) });
    vt.textContent = (fmt || num)(rows[i].value);
    svg.appendChild(vt);
  }
}

/* Value map: cost against delivery, bubble size = spend. Anything below the line beats the
   overall average. This is the chart that actually moves budget. */
function drawScatter(svgId, pts, opts){
  var svg = byId(svgId);
  if(!svg){ return; }
  clear(svg);
  opts = opts || {};
  var vb = (svg.getAttribute("viewBox") || "0 0 660 380").split(/\s+/);
  var W = Number(vb[2]), H = Number(vb[3]);
  var box = { x:54, y:14, w:W - 54 - 20, h:H - 14 - 34 };
  if(!pts.length){
    var t = el("text", { class:"axis", x:W / 2, y:H / 2, "text-anchor":"middle" });
    t.textContent = "No segments above the volume floor.";
    svg.appendChild(t);
    return;
  }
  var xm = 0, ym = 0, sm = 0, i;
  for(i = 0; i < pts.length; i++){
    if(n0(pts[i].x) > xm){ xm = n0(pts[i].x); }
    if(n0(pts[i].y) > ym){ ym = n0(pts[i].y); }
    if(n0(pts[i].size) > sm){ sm = n0(pts[i].size); }
  }
  xm = niceMax(xm); ym = niceMax(ym);
  gridY(svg, box, ym, opts.yFmt || compactN);
  var xt, i2;
  for(i2 = 0; i2 <= 4; i2++){
    xt = box.x + (i2 / 4) * box.w;
    var xl = el("text", { class:"axis", x:xt.toFixed(1), y:H - 12, "text-anchor":"middle" });
    xl.textContent = (opts.xFmt || compact)(xm * (i2 / 4));
    svg.appendChild(xl);
  }
  /* the average line: everything below it is cheaper than the account average */
  if(isNum(opts.avg) && opts.avg > 0 && opts.avg <= ym){
    var ay = box.y + box.h - (opts.avg / ym) * box.h;
    svg.appendChild(el("line", { class:"ref", x1:box.x, y1:ay.toFixed(1), x2:box.x + box.w, y2:ay.toFixed(1) }));
    var al = el("text", { class:"reflbl", x:box.x + 4, y:(ay - 5).toFixed(1) });
    al.textContent = opts.avgLabel || "account average";
    svg.appendChild(al);
  }
  for(i = 0; i < pts.length; i++){
    var p = pts[i];
    var cx = box.x + (n0(p.x) / xm) * box.w;
    var cy = box.y + box.h - (n0(p.y) / ym) * box.h;
    var rr = 5 + Math.sqrt(sm ? n0(p.size) / sm : 0) * 16;
    var c = el("circle", { cx:cx.toFixed(1), cy:cy.toFixed(1), r:rr.toFixed(1),
      fill:p.color || "var(--s1)", opacity:"0.62", stroke:p.color || "var(--s1)", "stroke-width":"1.4" });
    bindTip(c, tipTitle(p.label) +
      tipRow(opts.xLabel || "x", (opts.xFmt || compact)(p.x)) +
      tipRow(opts.yLabel || "y", (opts.yFmt || compactN)(p.y)) +
      tipRow(opts.sizeLabel || "Spend", compact(p.size)));
    svg.appendChild(c);
  }
}

/* Donut with a clickable legend that cross-filters the tab. Category colours are assigned ONCE
   by overall volume by the caller, so a slice keeps its colour across every filter and tab. */
function drawDonut(svgId, legendId, rows, fmt, onPick, selected){
  var svg = byId(svgId), leg = byId(legendId);
  if(!svg){ return; }
  clear(svg);
  var total = 0, i;
  for(i = 0; i < rows.length; i++){ total += n0(rows[i].value); }
  if(!total){
    if(leg){ leg.innerHTML = '<div class="dl"><span class="nm">No data in the selected period.</span></div>'; }
    return;
  }
  var cx = 100, cy = 100, r = 78, ir = 48, a0 = -Math.PI / 2;
  for(i = 0; i < rows.length; i++){
    var frac = n0(rows[i].value) / total;
    var a1 = a0 + frac * Math.PI * 2;
    var large = (a1 - a0) > Math.PI ? 1 : 0;
    var p = ["M", (cx + r * Math.cos(a0)).toFixed(2), (cy + r * Math.sin(a0)).toFixed(2),
             "A", r, r, 0, large, 1, (cx + r * Math.cos(a1)).toFixed(2), (cy + r * Math.sin(a1)).toFixed(2),
             "L", (cx + ir * Math.cos(a1)).toFixed(2), (cy + ir * Math.sin(a1)).toFixed(2),
             "A", ir, ir, 0, large, 0, (cx + ir * Math.cos(a0)).toFixed(2), (cy + ir * Math.sin(a0)).toFixed(2),
             "Z"].join(" ");
    var path = el("path", { d:p, fill:rows[i].color, opacity:(selected && selected !== rows[i].label) ? "0.32" : "1" });
    bindTip(path, tipTitle(rows[i].label) + tipRow("Share", (frac * 100).toFixed(1) + "%") +
      tipRow("Value", (fmt || num)(rows[i].value)));
    svg.appendChild(path);
    a0 = a1;
  }
  if(leg){
    var h = "";
    for(i = 0; i < rows.length; i++){
      var sh = (n0(rows[i].value) / total * 100).toFixed(1) + "%";
      h += '<div class="dl' + (selected === rows[i].label ? " sel" : (selected ? " off" : "")) +
           '" data-pick="' + esc(rows[i].label) + '" role="button" tabindex="0">' +
           '<span class="sw" style="background:' + rows[i].color + '"></span>' +
           '<span class="nm">' + esc(rows[i].label) + "</span>" +
           '<span class="vv">' + (fmt || num)(rows[i].value) + "</span>" +
           '<span class="pp">' + sh + "</span></div>";
    }
    leg.innerHTML = h;
    if(onPick && !leg.getAttribute("data-wired")){
      leg.setAttribute("data-wired", "1");
      leg.addEventListener("click", function(e){
        var n = e.target;
        while(n && n !== leg && !(n.getAttribute && n.getAttribute("data-pick"))){ n = n.parentNode; }
        if(n && n.getAttribute && n.getAttribute("data-pick")){ onPick(n.getAttribute("data-pick")); }
      });
    }
  }
}

/* Cross-tab heatmap -- the cut that tells you where to move money.
   cells: {rowKey: {colKey: value}} */
function drawHeat(hostId, rowKeys, colKeys, cells, fmt, opts){
  var host = byId(hostId);
  if(!host){ return; }
  opts = opts || {};
  if(!rowKeys.length || !colKeys.length){
    host.innerHTML = emptyState("No cross-tab yet",
      "This needs both breakdowns in the payload. It lights up the day the export job carries them.");
    return;
  }
  var lo = null, hi = null, i, j;
  for(i = 0; i < rowKeys.length; i++){
    for(j = 0; j < colKeys.length; j++){
      var v = cells[rowKeys[i]] && cells[rowKeys[i]][colKeys[j]];
      if(!isNum(v)){ continue; }
      if(lo === null || v < lo){ lo = v; }
      if(hi === null || v > hi){ hi = v; }
    }
  }
  var h = '<div class="hm" style="grid-template-columns:78px repeat(' + colKeys.length + ',minmax(0,1fr))">' +
          '<div class="hh"></div>';
  for(j = 0; j < colKeys.length; j++){ h += '<div class="hh">' + esc(colKeys[j]) + "</div>"; }
  for(i = 0; i < rowKeys.length; i++){
    h += '<div class="rl">' + esc(rowKeys[i]) + "</div>";
    for(j = 0; j < colKeys.length; j++){
      var val = cells[rowKeys[i]] && cells[rowKeys[i]][colKeys[j]];
      if(!isNum(val)){
        h += '<div class="cell" title="no volume">·</div>';
        continue;
      }
      /* sqrt the scale so mid-volume values stay visible; low is good for a cost measure */
      var t = (hi === lo) ? 0.5 : Math.sqrt((val - lo) / (hi - lo));
      var mixA = opts.lowIsGood === false ? "var(--crit)" : "var(--ok)";
      var mixB = opts.lowIsGood === false ? "var(--ok)" : "var(--crit)";
      var bg = "color-mix(in srgb, " + mixB + " " + Math.round(t * 72) + "%, " + mixA + ")";
      h += '<div class="cell" style="background:' + bg + ';color:#fff" title="' +
           esc(rowKeys[i] + " × " + colKeys[j]) + '">' + (fmt || num)(val) + "</div>";
    }
  }
  host.innerHTML = h + "</div>";
}

/* ===================================================================== modal (lightbox) */
function openModal(html){
  var m = byId("cMod");
  if(!m){ return; }
  setHTML("cModBody", html);
  m.classList.add("on");
}
function closeModal(){
  var m = byId("cMod");
  if(m){ m.classList.remove("on"); }
}
function wireModal(){
  var x = byId("cModX");
  if(x){ x.addEventListener("click", closeModal); }
  var m = byId("cMod");
  /* click the BACKDROP (never the card itself) to dismiss */
  if(m){ m.addEventListener("click", function(e){ if(e.target === m){ closeModal(); } }); }
}
