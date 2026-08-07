/* SentinelBank charts — hand-drawn SVG, zero dependencies.
 *
 * Deliberately not Chart.js or ECharts: the demo has to run with the wifi off, and a
 * chart library loaded from a CDN is the first thing to break.
 *
 * Colours are emitted as live `var(--token, fallback)` rather than resolved values, so
 * charts and chrome can never drift apart and a theme switch recolours every chart with
 * no redraw at all -- see paint() below.
 *
 * Every function takes (el, data, opts) and replaces el's contents.
 */
(function (global) {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";

  /* Theme-live paint. Deliberately does NOT resolve the variable: the literal
     "var(--x, fallback)" keeps the colour bound to the cascade, so flipping data-theme
     recolours every chart with no redraw, no registry, and -- critically -- no replayed
     entry animations. Charts used to bake getComputedStyle() results into attributes at
     draw time and would have gone stale on the first theme switch.

     The fallback is mandatory. An unresolvable var() in a paint slot does not fall back to
     a sensible default -- it computes to BLACK, which on a dark page is an invisible chart.
     _harness.html asserts no chart node computes to rgb(0,0,0) for exactly this reason. */
  function paint(name, fallback) { return `var(${name}, ${fallback})`; }

  function palette() {
    return [
      paint("--info", "#60a5fa"), paint("--violet", "#a78bfa"),
      paint("--ok", "#34d399"), paint("--warn", "#fbbf24"),
      paint("--danger", "#f87171"), paint("--cyan", "#22d3ee"),
      paint("--pink", "#f472b6"), paint("--lime", "#a3e635"),
    ];
  }

  /* Paint attributes carrying a var() go through style, not setAttribute.
     Two reasons. A presentation attribute LOSES to a stylesheet rule, so `fill="var(--x)"`
     would be overridden by .chart text{fill:...} in glass.css, whereas an inline style wins
     -- that preserves the precedence the code already had. And var() in a presentation
     attribute is not universally supported, while an inline style is unambiguously CSS.
     This also transparently upgrades the templates that already pass colour:"var(--ok)". */
  const PAINT = { fill: 1, stroke: 1, "stop-color": 1, "flood-color": 1 };

  function el(tag, attrs, text) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) {
      const v = attrs[k];
      if (PAINT[k] && typeof v === "string" && v.lastIndexOf("var(", 0) === 0) {
        n.style.setProperty(k, v);
      } else {
        n.setAttribute(k, v);
      }
    }
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function svg(host, w, h) {
    host.innerHTML = "";
    const s = el("svg", {
      viewBox: `0 0 ${w} ${h}`, class: "chart",
      preserveAspectRatio: "xMidYMid meet", role: "img",
    });
    host.appendChild(s);
    return s;
  }

  /* Chart-internal abbreviation. Indian crore/lakh, because the demo bank is Indian and
     every axis label is INR. Left exactly as it was on purpose -- it is called from inside
     bars(), stackedBars() and areaLine() for gridlines and tooltips, and changing its
     output would shift measurements those charts already depend on. */
  function fmt(n) {
    const a = Math.abs(n);
    if (a >= 1e7) return (n / 1e7).toFixed(1) + "Cr";
    if (a >= 1e5) return (n / 1e5).toFixed(1) + "L";
    if (a >= 1000) return (n / 1000).toFixed(1) + "k";
    return Math.round(n).toString();
  }

  /* The currency-aware one. `fmt` abbreviates 12,000,000 as "1.2Cr" whatever the currency,
     so a USD chart read "1.2Cr" -- crore and lakh are an Indian convention and mean nothing
     against a dollar figure. Use this anywhere the currency is known; use fmt() for bare
     chart internals. */
  function currencySymbol(code) {
    const table = window.SB_CURRENCY || {};   /* declared in core/money.py, injected by base.html */
    const c = String(code || "").toUpperCase();
    return table[c] || c;
  }

  function fmtMoney(n, code) {
    const c = String(code || "").toUpperCase();
    const a = Math.abs(n);
    let body;
    if (c === "INR") {
      body = a >= 1e7 ? (n / 1e7).toFixed(1) + "Cr"
           : a >= 1e5 ? (n / 1e5).toFixed(1) + "L"
           : a >= 1000 ? (n / 1000).toFixed(1) + "k"
           : Math.round(n).toString();
    } else {
      body = a >= 1e9 ? (n / 1e9).toFixed(1) + "B"
           : a >= 1e6 ? (n / 1e6).toFixed(1) + "M"
           : a >= 1000 ? (n / 1000).toFixed(1) + "K"
           : Math.round(n).toString();
    }
    if (!c) return body;
    const sym = currencySymbol(c);
    return /^[A-Z]{2,}$/.test(sym) ? `${sym} ${body}` : `${sym}${body}`;
  }

  function title(node, text) {
    node.appendChild(el("title", {}, text));
    return node;
  }

  function gradeColour(score) {
    if (score >= 88) return paint("--ok", "#34d399");
    if (score >= 72) return paint("--lime", "#a3e635");
    if (score >= 55) return paint("--warn", "#fbbf24");
    return paint("--danger", "#f87171");
  }

  /* ------------------------------------------------------------------ ring */
  /* Score ring. Uses stroke-dasharray rather than conic-gradient so it animates and
     works identically when printed. */
  function ring(host, score, opts) {
    opts = opts || {};
    const size = 118, sw = 11, r = (size - sw) / 2, c = 2 * Math.PI * r;
    const colour = opts.colour || gradeColour(score);
    const s = svg(host, size, size);
    s.setAttribute("style", "width:100%;height:100%");

    s.appendChild(el("circle", {
      cx: size / 2, cy: size / 2, r: r, fill: "none",
      stroke: paint("--glass-3", "rgba(255,255,255,.11)"), "stroke-width": sw,
    }));

    const arc = el("circle", {
      cx: size / 2, cy: size / 2, r: r, fill: "none", stroke: colour,
      "stroke-width": sw, "stroke-linecap": "round",
      "stroke-dasharray": c, "stroke-dashoffset": c,
      transform: `rotate(-90 ${size / 2} ${size / 2})`,
    });
    arc.style.transition = "stroke-dashoffset .9s cubic-bezier(.22,.9,.3,1)";
    s.appendChild(arc);
    requestAnimationFrame(() => {
      arc.setAttribute("stroke-dashoffset", c * (1 - Math.max(0, Math.min(100, score)) / 100));
    });
    return colour;
  }

  /* ----------------------------------------------------------------- donut */
  function donut(host, data, opts) {
    opts = opts || {};
    const size = 230, sw = 34, r = (size - sw) / 2 - 4, cx = size / 2, cy = size / 2;
    const total = data.reduce((a, d) => a + d.value, 0) || 1;
    const cols = palette();
    const s = svg(host, size, size);

    let angle = -Math.PI / 2;
    data.forEach((d, i) => {
      const frac = d.value / total;
      const sweep = frac * 2 * Math.PI;
      const end = angle + sweep;
      const large = sweep > Math.PI ? 1 : 0;
      const x1 = cx + r * Math.cos(angle), y1 = cy + r * Math.sin(angle);
      const x2 = cx + r * Math.cos(end), y2 = cy + r * Math.sin(end);
      const colour = d.colour || cols[i % cols.length];

      const path = el("path", {
        d: `M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2}`,
        fill: "none", stroke: colour, "stroke-width": sw,
      });
      path.style.transition = "stroke-width .15s";
      path.addEventListener("mouseenter", () => path.setAttribute("stroke-width", sw + 6));
      path.addEventListener("mouseleave", () => path.setAttribute("stroke-width", sw));
      title(path, `${d.label}: ${fmt(d.value)} (${(frac * 100).toFixed(1)}%)`);
      s.appendChild(path);
      angle = end;
    });

    if (opts.centreLabel) {
      s.appendChild(el("text", {
        x: cx, y: cy - 4, "text-anchor": "middle",
        style: `font-size:20px;font-weight:800;fill:${paint("--ink", "#fff")}`,
      }, opts.centreLabel));
      if (opts.centreSub) {
        s.appendChild(el("text", { x: cx, y: cy + 14, "text-anchor": "middle",
          style: "font-size:10px" }, opts.centreSub));
      }
    }
    return legend(host, data, cols);
  }

  function legend(host, data, cols) {
    cols = cols || palette();
    const box = document.createElement("div");
    box.className = "legend";
    box.style.marginTop = ".6rem";
    box.style.justifyContent = "center";
    data.forEach((d, i) => {
      const item = document.createElement("span");
      item.innerHTML = `<i style="background:${d.colour || cols[i % cols.length]}"></i>${d.label}`;
      box.appendChild(item);
    });
    host.appendChild(box);
    return box;
  }

  /* ------------------------------------------------------------------ bars */
  function bars(host, data, opts) {
    opts = opts || {};
    const w = 520, h = opts.height || 220, pad = { l: 40, r: 10, t: 14, b: 26 };
    const iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
    const max = Math.max(...data.map(d => d.value), 1);
    const bw = iw / data.length;
    const s = svg(host, w, h);

    for (let i = 0; i <= 4; i++) {
      const y = pad.t + ih - (ih * i / 4);
      s.appendChild(el("line", { x1: pad.l, y1: y, x2: w - pad.r, y2: y, class: "grid-line" }));
      s.appendChild(el("text", { x: pad.l - 6, y: y + 3, "text-anchor": "end" }, fmt(max * i / 4)));
    }

    data.forEach((d, i) => {
      const bh = (d.value / max) * ih;
      const x = pad.l + i * bw + bw * 0.18;
      const bwidth = bw * 0.64;
      const colour = d.colour || opts.colour || paint("--info", "#60a5fa");
      const rect = el("rect", {
        x: x, y: pad.t + ih, width: bwidth, height: 0, rx: 5, fill: colour, opacity: .85,
      });
      rect.style.transition = "y .6s cubic-bezier(.22,.9,.3,1), height .6s cubic-bezier(.22,.9,.3,1), opacity .15s";
      rect.addEventListener("mouseenter", () => rect.setAttribute("opacity", 1));
      rect.addEventListener("mouseleave", () => rect.setAttribute("opacity", .85));
      title(rect, `${d.label}: ${fmt(d.value)}`);
      s.appendChild(rect);
      requestAnimationFrame(() => {
        rect.setAttribute("y", pad.t + ih - bh);
        rect.setAttribute("height", Math.max(1, bh));
      });
      if (data.length <= 14) {
        /* Fit the label to the bar rather than to a fixed 7 characters. The old rule
           also kept the LAST 5 chars, so "Legitimate" rendered as "imate" and "AMERICAS"
           as "ricas" -- on two bars with 260px each to play with. Keep the head, and only
           elide when the width genuinely cannot take it. */
        const maxChars = Math.max(4, Math.floor(bw / 6.2));
        const label = d.label.length > maxChars
          ? d.label.slice(0, maxChars - 1) + "…"
          : d.label;
        s.appendChild(el("text", {
          x: x + bwidth / 2, y: h - 8, "text-anchor": "middle",
        }, label));
      }
    });
  }

  /* ---------------------------------------------------------- stackedBars */
  function stackedBars(host, data, opts) {
    opts = opts || {};
    const w = 520, h = opts.height || 240, pad = { l: 110, r: 14, t: 10, b: 22 };
    const iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
    const max = Math.max(...data.map(d => d.total), 1);
    const bh = ih / data.length;
    const cols = palette();
    const s = svg(host, w, h);

    data.forEach((d, i) => {
      const y = pad.t + i * bh + bh * 0.2;
      const height = bh * 0.6;
      let x = pad.l;
      d.parts.forEach((p, j) => {
        const pw = (p.value / max) * iw;
        const rect = el("rect", {
          x: x, y: y, width: Math.max(0, pw), height: height,
          fill: p.colour || cols[j % cols.length], opacity: p.dim ? .32 : .9,
          rx: 3,
        });
        title(rect, `${d.label} — ${p.label}: ${fmt(p.value)}`);
        s.appendChild(rect);
        x += pw;
      });
      s.appendChild(el("text", {
        x: pad.l - 8, y: y + height / 2 + 3, "text-anchor": "end",
      }, d.label.length > 15 ? d.label.slice(0, 14) + "…" : d.label));
    });
  }

  /* -------------------------------------------------------------- areaLine */
  /* Daily spend with a cumulative overlay and reference lines. */
  function areaLine(host, series, opts) {
    opts = opts || {};
    const w = 720, h = opts.height || 260, pad = { l: 46, r: 16, t: 16, b: 28 };
    const iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
    const pts = series.points || [];
    if (!pts.length) { host.innerHTML = '<p class="dim small">No data yet.</p>'; return; }

    const refs = (opts.refs || []).map(r => r.value);
    const max = Math.max(...pts.map(p => p.value), ...refs, 1) * 1.08;
    const s = svg(host, w, h);

    for (let i = 0; i <= 4; i++) {
      const y = pad.t + ih - (ih * i / 4);
      s.appendChild(el("line", { x1: pad.l, y1: y, x2: w - pad.r, y2: y, class: "grid-line" }));
      s.appendChild(el("text", { x: pad.l - 6, y: y + 3, "text-anchor": "end" }, fmt(max * i / 4)));
    }

    const X = i => pad.l + (pts.length === 1 ? iw / 2 : (i / (pts.length - 1)) * iw);
    const Y = v => pad.t + ih - (v / max) * ih;

    if (opts.barsToo) {
      const bw = Math.max(2, iw / pts.length * 0.5);
      pts.forEach((p, i) => {
        const bh = (p.barValue !== undefined ? p.barValue : p.value) / max * ih;
        const rect = el("rect", {
          x: X(i) - bw / 2, y: pad.t + ih - bh, width: bw, height: Math.max(1, bh),
          fill: paint("--info", "#60a5fa"), opacity: .28, rx: 2,
        });
        title(rect, `${p.label}: ${fmt(p.barValue !== undefined ? p.barValue : p.value)}`);
        s.appendChild(rect);
      });
    }

    const line = pts.map((p, i) => `${i ? "L" : "M"} ${X(i)} ${Y(p.value)}`).join(" ");
    const area = `${line} L ${X(pts.length - 1)} ${pad.t + ih} L ${X(0)} ${pad.t + ih} Z`;

    const gid = "grad" + Math.random().toString(36).slice(2, 8);
    const defs = el("defs", {});
    const lg = el("linearGradient", { id: gid, x1: "0", y1: "0", x2: "0", y2: "1" });
    lg.appendChild(el("stop", { offset: "0%", "stop-color": paint("--violet", "#a78bfa"), "stop-opacity": ".45" }));
    lg.appendChild(el("stop", { offset: "100%", "stop-color": paint("--violet", "#a78bfa"), "stop-opacity": "0" }));
    defs.appendChild(lg); s.appendChild(defs);

    s.appendChild(el("path", { d: area, fill: `url(#${gid})`, stroke: "none" }));

    const stroke = el("path", {
      d: line, fill: "none", stroke: paint("--violet", "#a78bfa"),
      "stroke-width": 2.5, "stroke-linejoin": "round", "stroke-linecap": "round",
    });
    /* Measure the path instead of guessing at it. This was a hardcoded 2000, which is only
       ever right by accident: a short series finished drawing instantly because the dash
       already covered it, and a long one never finished inside the 1s transition. The path
       has to be in the document before getTotalLength() will measure it. */
    s.appendChild(stroke);
    const len = Math.ceil(stroke.getTotalLength()) || 1;
    stroke.setAttribute("stroke-dasharray", len);
    stroke.setAttribute("stroke-dashoffset", len);
    stroke.style.transition = "stroke-dashoffset 1s ease-out";
    requestAnimationFrame(() => stroke.setAttribute("stroke-dashoffset", 0));

    pts.forEach((p, i) => {
      const dot = el("circle", { cx: X(i), cy: Y(p.value), r: 3.2,
        fill: paint("--bg-1", "#0b1120"), stroke: paint("--violet", "#a78bfa"), "stroke-width": 2 });
      title(dot, `${p.label}: ${fmt(p.value)}`);
      s.appendChild(dot);
    });

    (opts.refs || []).forEach(rf => {
      const y = Y(rf.value);
      s.appendChild(el("line", {
        x1: pad.l, y1: y, x2: w - pad.r, y2: y,
        stroke: rf.colour || paint("--warn", "#fbbf24"),
        "stroke-width": 1.5, "stroke-dasharray": "5 4", opacity: .85,
      }));
      s.appendChild(el("text", {
        x: w - pad.r, y: y - 5, "text-anchor": "end",
        style: `fill:${rf.colour || paint("--warn", "#fbbf24")};font-size:9.5px;font-weight:700`,
      }, `${rf.label} ${fmt(rf.value)}`));
    });

    const step = Math.ceil(pts.length / 8);
    pts.forEach((p, i) => {
      if (i % step === 0) {
        s.appendChild(el("text", { x: X(i), y: h - 8, "text-anchor": "middle" }, p.label));
      }
    });
  }

  /* ------------------------------------------------------------ sparkline */
  function sparkline(host, values, opts) {
    opts = opts || {};
    const w = 120, h = 30;
    if (!values.length) { host.innerHTML = ""; return; }
    const max = Math.max(...values, 1), min = Math.min(...values, 0);
    const span = (max - min) || 1;
    const s = svg(host, w, h);
    const d = values.map((v, i) =>
      `${i ? "L" : "M"} ${(i / (values.length - 1 || 1)) * w} ${h - ((v - min) / span) * (h - 4) - 2}`
    ).join(" ");
    s.appendChild(el("path", {
      d: d, fill: "none", stroke: opts.colour || paint("--info", "#60a5fa"),
      "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round",
    }));
  }

  /* ------------------------------------------------------------- heatGrid */
  /* Activity intensity by weekday × hour band. Reads as "when does this person spend". */
  function heatGrid(host, cells, opts) {
    opts = opts || {};
    const cols = opts.cols || 7, rows = opts.rows || 4;
    const cw = 30, ch = 22, gap = 3;
    const w = cols * (cw + gap) + 46, h = rows * (ch + gap) + 24;
    const max = Math.max(...cells.map(c => c.value), 1);
    const s = svg(host, w, h);
    const base = paint("--info", "#60a5fa");

    cells.forEach(c => {
      const x = 44 + c.col * (cw + gap);
      const y = 16 + c.row * (ch + gap);
      const rect = el("rect", {
        x: x, y: y, width: cw, height: ch, rx: 4,
        fill: base, opacity: 0.08 + 0.85 * (c.value / max),
      });
      title(rect, `${c.rowLabel} ${c.colLabel}: ${fmt(c.value)}`);
      s.appendChild(rect);
    });
    (opts.colLabels || []).forEach((l, i) =>
      s.appendChild(el("text", { x: 44 + i * (cw + gap) + cw / 2, y: 10, "text-anchor": "middle" }, l)));
    (opts.rowLabels || []).forEach((l, i) =>
      s.appendChild(el("text", { x: 40, y: 16 + i * (ch + gap) + ch / 2 + 3, "text-anchor": "end" }, l)));
  }

  /* ---------------------------------------------------------------- gauge */
  /* The 17px of slack above the arc and 11px below is deliberate, not sloppy centring:
     opts.sub renders at baseline cy+6 = 106 at 10px, so its descenders reach ~108 in a
     118-tall box. Rebalancing the arc vertically clips the sub-label. */
  function gauge(host, value, opts) {
    opts = opts || {};
    const w = 200, h = 118, cx = w / 2, cy = 100, r = 76, sw = 14;
    const s = svg(host, w, h);
    const pct = Math.max(0, Math.min(1, value / (opts.max || 100)));

    /* f maps onto the TOP semicircle (a = PI + f*PI), so the sweep is (to-from)*180deg and
       can never exceed 180. The large-arc-flag is therefore always 0.
       It used to be `(to - from) > .5 ? 1 : 0`, copied from donut() where the fraction is of
       a full turn. Here it flipped to 1 at a 90deg sweep -- i.e. any value above 50 -- and
       with sweep-flag 1 the renderer then picks the *other* candidate circle centre and
       draws the complementary 360-sweep arc, which detaches from the track and is clipped by
       the viewBox. The grey track is exactly 180deg, so both of its candidate centres
       degenerate to the same point and it always rendered correctly. That is why only the
       coloured arc jumped, and why this went unnoticed. Do not "restore" the ternary. */
    const arcPath = (from, to) => {
      const a1 = Math.PI + from * Math.PI, a2 = Math.PI + to * Math.PI;
      const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
      const x2 = cx + r * Math.cos(a2), y2 = cy + r * Math.sin(a2);
      return `M ${x1} ${y1} A ${r} ${r} 0 0 1 ${x2} ${y2}`;
    };

    s.appendChild(el("path", {
      d: arcPath(0, 1), fill: "none", "stroke-width": sw, "stroke-linecap": "round",
      stroke: paint("--glass-3", "rgba(255,255,255,.11)"),
    }));
    s.appendChild(el("path", {
      d: arcPath(0, Math.max(0.001, pct)), fill: "none", "stroke-width": sw,
      "stroke-linecap": "round", stroke: opts.colour || gradeColour(value),
    }));
    s.appendChild(el("text", {
      x: cx, y: cy - 12, "text-anchor": "middle",
      style: `font-size:24px;font-weight:800;fill:${paint("--ink", "#fff")}`,
    }, opts.display || String(Math.round(value))));
    if (opts.sub) {
      s.appendChild(el("text", { x: cx, y: cy + 6, "text-anchor": "middle", style: "font-size:10px" }, opts.sub));
    }
  }

  global.SBCharts = {
    ring, donut, bars, stackedBars, areaLine, sparkline, heatGrid, gauge,
    palette, fmt, fmtMoney, currencySymbol, gradeColour, legend,
  };
})(window);
