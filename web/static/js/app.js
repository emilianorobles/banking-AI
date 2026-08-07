/* BedRock Financial front-end runtime: fetch helpers, toasts, modals, drill-downs,
 * balance masking, notification polling. No framework, no build step. */
(function (global) {
  "use strict";

  /* ------------------------------------------------------------------ http */
  async function getJSON(url) {
    const r = await fetch(url, { headers: { "Accept": "application/json" } });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  }

  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
    return data;
  }

  /* ---------------------------------------------------------------- toasts */
  const ICONS = { ok: "✅", info: "🔔", warn: "⚠️", danger: "🚨" };

  function toast(title, body, kind, ms) {
    kind = kind || "info";
    const host = document.getElementById("toast-host");
    if (!host) return;
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.innerHTML =
      `<div class="t-title">${ICONS[kind] || "🔔"} ${escapeHtml(title)}</div>` +
      (body ? `<div class="t-body">${escapeHtml(body)}</div>` : "");
    host.appendChild(t);
    const life = ms || 6000;
    setTimeout(() => {
      t.style.transition = "opacity .3s, transform .3s";
      t.style.opacity = "0";
      t.style.transform = "translateX(20px)";
      setTimeout(() => t.remove(), 320);
    }, life);
    return t;
  }

  /* ---------------------------------------------------------------- modals */
  function openModal(html, opts) {
    opts = opts || {};
    const host = document.getElementById("modal-host");
    if (!host) return;
    host.innerHTML =
      `<div class="modal ${opts.large ? "modal-lg" : ""}" role="dialog" aria-modal="true">
         <div class="modal-head">
           <div>
             <h3 style="margin:0">${escapeHtml(opts.title || "")}</h3>
             ${opts.subtitle ? `<div class="small dim">${escapeHtml(opts.subtitle)}</div>` : ""}
           </div>
           <button class="x" data-close aria-label="Close">&times;</button>
         </div>
         <div class="modal-body">${html}</div>
       </div>`;
    host.classList.add("open");
    host.querySelector("[data-close]").addEventListener("click", closeModal);
    /* Drill targets inside modal content were dead clicks: wireDrilldowns() only ever ran
       once on DOMContentLoaded, so anything injected later was never bound. It is
       idempotent via dataset.drillWired, so this central hook is safe and means a drill
       inside a drill cannot be forgotten. */
    wireDrilldowns(host);
    document.body.style.overflow = "hidden";
  }

  function closeModal() {
    const host = document.getElementById("modal-host");
    if (!host) return;
    host.classList.remove("open");
    host.innerHTML = "";
    document.body.style.overflow = "";
  }

  function loadingModal(title) {
    openModal('<p class="dim">Loading…</p>', { title });
  }

  document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });
  document.addEventListener("click", e => {
    const host = document.getElementById("modal-host");
    if (host && e.target === host) closeModal();
  });

  /* ------------------------------------------------------------- drilldown */

  /* Charts the server is allowed to ask for, by name. A LITERAL map, deliberately -- not
     `SBCharts[spec.type]`, which would let a payload name any property on the object
     including `constructor` and `__proto__`. Resolved at call time rather than captured at
     definition time so load order between charts.js and app.js cannot matter. */
  const DRILL_CHARTS = {
    ring:        (h, d, o) => global.SBCharts.ring(h, d, o),
    donut:       (h, d, o) => global.SBCharts.donut(h, d, o),
    bars:        (h, d, o) => global.SBCharts.bars(h, d, o),
    stackedBars: (h, d, o) => global.SBCharts.stackedBars(h, d, o),
    areaLine:    (h, d, o) => global.SBCharts.areaLine(h, d, o),
    sparkline:   (h, d, o) => global.SBCharts.sparkline(h, d, o),
    heatGrid:    (h, d, o) => global.SBCharts.heatGrid(h, d, o),
    gauge:       (h, d, o) => global.SBCharts.gauge(h, d, o),
  };

  /* The drill envelope carries DATA, never markup -- renderDrill escapes every value it is
     given, and that is the property worth keeping, because the strings inside a payload
     include merchant names that came from outside the bank. So a chart arrives as
     {type, data, opts} and is drawn here, and a section arrives as a kind plus fields.

     Charts cannot be drawn during renderDrill because it returns a string and SBCharts
     needs a live DOM node. renderDrill therefore emits numbered placeholders and
     paintCharts fills them after the HTML is in the document. Both derive the ordering
     from chartSpecs(), so the numbering cannot drift. */
  function chartSpecs(d) {
    const specs = [];
    (d.charts || []).forEach(c => specs.push(c));
    (d.sections || []).forEach(s => {
      if (s && s.kind === "chart") {
        specs.push({ type: s.chart_type, title: s.title, data: s.data,
                     opts: s.opts, height: s.height });
      }
    });
    return specs;
  }

  function chartSlot(i, spec) {
    const style = spec && spec.height ? ` style="min-height:${Number(spec.height)}px"` : "";
    return `<div class="chart-slot" data-chart-slot="${i}"${style}></div>`;
  }

  /* Second caller is the chat widget (tool result visuals), which is why this takes a
     spec LIST rather than a drill payload. */
  function paintCharts(specs, root) {
    if (!specs || !specs.length) return;
    const host = root || document;
    specs.forEach((spec, i) => {
      const slot = host.querySelector(`[data-chart-slot="${i}"]`);
      if (!slot || !spec) return;
      const draw = Object.prototype.hasOwnProperty.call(DRILL_CHARTS, spec.type)
        ? DRILL_CHARTS[spec.type] : null;
      if (!draw || !global.SBCharts) {
        slot.innerHTML = '<p class="dim small">Chart unavailable.</p>';
        return;
      }
      try {
        draw(slot, spec.data, spec.opts || {});
      } catch (err) {
        /* One bad chart must not blank the rest of the explanation. */
        slot.innerHTML = '<p class="dim small">Could not draw this chart.</p>';
      }
    });
  }

  /* ---- section kinds. Unknown kinds render nothing, so the server can add one before
     every client has reloaded. --------------------------------------------------- */
  const TONE_PILL = { ok: "pill-ok", info: "pill-info", warn: "pill-warn",
                      danger: "pill-danger", critical: "pill-critical", violet: "pill-violet" };
  const TONE_NOTE = { ok: "note-ok", info: "", warn: "note-warn", danger: "note-danger" };
  /* Three states, and none of them is a green tick. A check that did not fire was
     evaluated and found not to apply -- it was not "verified", and claiming otherwise in a
     bank UI is a promise the system has not made. */
  const CHECK_ICON = { fired: "●", suppressed: "◐", clear: "○" };
  const CHECK_TONE = { fired: "pill-danger", suppressed: "pill-info", clear: "" };

  function sectionHtml(s, slotOf) {
    if (!s || !s.kind) return "";
    const h = s.title ? `<h4 style="margin-top:1rem">${escapeHtml(s.title)}</h4>` : "";

    switch (s.kind) {
      case "verdict":
        return `<div class="row" style="gap:.5rem;margin-bottom:.35rem">
                  <span class="pill ${TONE_PILL[s.tone] || "pill-info"}">${escapeHtml(s.headline || "")}</span>
                </div>
                ${s.plain ? `<p style="margin:.2rem 0 1rem">${escapeHtml(s.plain)}</p>` : ""}`;

      case "text":
        return h + `<p class="muted small">${escapeHtml(s.body || "")}</p>`;

      case "chart":
        return h + chartSlot(slotOf(), s) +
               (s.caption ? `<p class="dim tiny" style="margin-top:.3rem">${escapeHtml(s.caption)}</p>` : "");

      case "table": {
        /* Explicit columns, not a union of row keys: fixed order, right-aligned money via
           the existing .tbl .num, and a row may omit a column without shifting the header. */
        const cols = s.columns || [];
        if (!cols.length || !(s.rows || []).length) return "";
        return h + `<div class="tbl-wrap"><table class="tbl"><thead><tr>` +
          cols.map(c => `<th${c.num ? ' class="num"' : ""}>${escapeHtml(c.label || prettyKey(c.key))}</th>`).join("") +
          `</tr></thead><tbody>` +
          s.rows.map(row => `<tr>` + cols.map(c => {
            const v = row[c.key];
            return `<td${c.num ? ' class="num"' : ""}>${escapeHtml(v === null || v === undefined ? "—" : v)}</td>`;
          }).join("") + `</tr>`).join("") +
          `</tbody></table></div>` +
          (s.note ? `<p class="dim tiny" style="margin-top:.35rem">${escapeHtml(s.note)}</p>` : "");
      }

      case "kv": {
        const entries = Array.isArray(s.items) ? s.items : Object.entries(s.items || {});
        if (!entries.length) return "";
        return h + `<dl class="kv">` + entries.map(([k, v]) =>
          `<dt>${escapeHtml(prettyKey(k))}</dt><dd>${escapeHtml(v)}</dd>`).join("") + `</dl>`;
      }

      case "checklist": {
        const items = s.items || [];
        if (!items.length) return "";
        return h +
          (s.subtitle ? `<p class="dim tiny" style="margin:-.3rem 0 .4rem">${escapeHtml(s.subtitle)}</p>` : "") +
          items.map(it => {
            const tone = CHECK_TONE[it.state] || "";
            return `<div class="checkrow">
                      <span class="ic ${tone ? "" : "dim"}"
                            style="${tone ? `color:var(--${it.state === "fired" ? "danger" : "info"})` : ""}"
                            aria-hidden="true">${CHECK_ICON[it.state] || "○"}</span>
                      <div style="min-width:0">
                        <div class="small"><strong>${escapeHtml(it.label || "")}</strong></div>
                        ${it.detail ? `<div class="tiny dim" style="margin-top:.15rem">${escapeHtml(it.detail)}</div>` : ""}
                      </div>
                    </div>`;
          }).join("");
      }

      case "formula":
        return h + `<pre class="code">${escapeHtml(s.body || "")}</pre>`;

      case "note":
        return `<div class="note ${TONE_NOTE[s.tone] === undefined ? "" : TONE_NOTE[s.tone]}"
                     style="margin-top:1rem">${escapeHtml(s.body || "")}</div>`;

      case "pills": {
        const items = s.items || [];
        if (!items.length) return "";
        return h + `<div class="row" style="margin-top:.4rem">` + items.map(p => {
          const cls = TONE_PILL[p.tone] || "pill-info";
          /* Clickable only when the server named a drill target -- and it works inside the
             modal because openModal() re-runs wireDrilldowns() over its own content. */
          return p.drill
            ? `<button class="pill ${cls}" style="cursor:pointer;border:0;font-family:inherit"
                       data-drill="${escapeHtml(p.drill)}"
                       data-drill-title="${escapeHtml(p.label || "")}">${escapeHtml(p.label || "")}</button>`
            : `<span class="pill ${cls}">${escapeHtml(p.label || "")}</span>`;
        }).join("") + `</div>`;
      }

      default:
        return "";
    }
  }

  /* Every health and security card carries data-drill="<kind>:<key>". Clicking it asks
     the server what actually went into that number and renders the working. This is the
     whole reason the migration happened, so it is deliberately generic and reusable. */
  function renderDrill(d) {
    let html = "";

    /* Top-level charts render above everything, before the seven legacy blocks. */
    let slot = 0;
    const nextSlot = () => slot++;
    (d.charts || []).forEach(c => {
      html += (c.title ? `<h4>${escapeHtml(c.title)}</h4>` : "") + chartSlot(nextSlot(), c) +
              (c.caption ? `<p class="dim tiny" style="margin-top:.3rem">${escapeHtml(c.caption)}</p>` : "");
    });

    /* `sections` is additive. When a handler sends one, it owns the whole body and can be
       laid out however that explanation wants; when it does not, the original seven fixed
       blocks render exactly as before. That is what keeps the seven drill kinds nobody is
       redesigning at zero regression risk. */
    if (d.sections && d.sections.length) {
      d.sections.forEach(s => { html += sectionHtml(s, nextSlot); });
      return html || '<p class="dim">No detail available.</p>';
    }


    if (d.headline) {
      html += `<div class="row" style="gap:.5rem;margin-bottom:.9rem">
                 <span class="pill ${d.headline_class || "pill-info"}">${escapeHtml(d.headline)}</span>
                 ${d.grade ? `<span class="dim small">${escapeHtml(d.grade)}</span>` : ""}
               </div>`;
    }
    if (d.what) {
      html += `<h4>What this measures</h4><p class="muted small">${escapeHtml(d.what)}</p>`;
    }
    if (d.inputs && Object.keys(d.inputs).length) {
      html += `<h4>The numbers that went in</h4><dl class="kv">`;
      for (const k in d.inputs) {
        html += `<dt>${escapeHtml(prettyKey(k))}</dt><dd>${escapeHtml(String(d.inputs[k]))}</dd>`;
      }
      html += `</dl>`;
    }
    if (d.formula) {
      html += `<h4 style="margin-top:1rem">How it was calculated</h4>
               <pre class="code">${escapeHtml(d.formula)}</pre>`;
    }
    if (d.evidence && d.evidence.length) {
      /* Union of keys across every row, in first-seen order. Taking the columns from
         evidence[0] alone silently dropped any field the first record happened not to
         carry, and shifted the rest of that row's cells under the wrong headers. The
         "—" fallback below already covers the gaps. */
      const cols = [];
      d.evidence.forEach(row => Object.keys(row).forEach(k => { if (!cols.includes(k)) cols.push(k); }));
      html += `<h4 style="margin-top:1rem">The records this came from</h4>
               <div class="tbl-wrap"><table class="tbl"><thead><tr>` +
        cols.map(c => `<th>${escapeHtml(prettyKey(c))}</th>`).join("") +
        `</tr></thead><tbody>` +
        d.evidence.map(row => `<tr>` + cols.map(c =>
          `<td>${escapeHtml(String(row[c] === null || row[c] === undefined ? "—" : row[c]))}</td>`).join("") +
          `</tr>`).join("") +
        `</tbody></table></div>`;
    }
    if (d.remediation) {
      html += `<h4 style="margin-top:1rem">How to improve this</h4>
               <div class="note note-${d.passed === false ? "warn" : "ok"}">${escapeHtml(d.remediation)}</div>`;
    }
    if (d.note) {
      html += `<p class="dim tiny" style="margin-top:1rem">${escapeHtml(d.note)}</p>`;
    }
    return html || '<p class="dim">No detail available.</p>';
  }

  function prettyKey(k) {
    return String(k).replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
  }

  async function drill(kind, key, title) {
    loadingModal(title || "Detail");
    try {
      const d = await getJSON(`/api/drill/${encodeURIComponent(kind)}/${encodeURIComponent(key)}`);
      openModal(renderDrill(d), { title: d.label || title || "Detail", subtitle: d.subtitle || "", large: true });
      /* After openModal, never before: the placeholders have to be in the document before
         SBCharts can be handed a node to draw into. */
      paintCharts(chartSpecs(d), document.getElementById("modal-host"));
    } catch (err) {
      openModal(`<div class="note note-danger">Could not load detail: ${escapeHtml(err.message)}</div>`,
                { title: title || "Detail" });
    }
  }

  function wireDrilldowns(root) {
    (root || document).querySelectorAll("[data-drill]").forEach(node => {
      if (node.dataset.drillWired) return;
      node.dataset.drillWired = "1";
      node.setAttribute("tabindex", "0");
      node.setAttribute("role", "button");
      const [kind, key] = node.dataset.drill.split(":");
      const label = node.dataset.drillTitle || "";
      const go = () => drill(kind, key, label);
      node.addEventListener("click", go);
      node.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
      });
    });
  }

  /* ----------------------------------------------------------------- mask */
  /* Balance hidden by default, like every real banking app. Persisted so it does not
     reset on navigation. */
  const MASK_KEY = "sb.maskBalance";

  function applyMask() {
    const masked = localStorage.getItem(MASK_KEY) !== "0";
    document.querySelectorAll(".maskable").forEach(n => n.classList.toggle("masked", masked));
    document.querySelectorAll("[data-eye]").forEach(b => {
      b.textContent = masked ? "👁" : "🙈";
      b.setAttribute("aria-label", masked ? "Show balance" : "Hide balance");
    });
  }

  function toggleMask() {
    const masked = localStorage.getItem(MASK_KEY) !== "0";
    localStorage.setItem(MASK_KEY, masked ? "0" : "1");
    applyMask();
  }

  /* ---------------------------------------------------------------- theme */
  /* Two states the user can pick, plus "no choice", which follows the OS. The stored value
     is the *preference*, not the resolved theme: absent means "keep following the system",
     so a customer who never touches the toggle tracks their laptop for good. Same contract
     as MASK_KEY above.
     The attribute itself is set pre-paint by partials/theme_boot.html; everything here is
     just keeping the button glyph honest and reacting to clicks. */
  const THEME_KEY = "sb.theme";

  function storedTheme() {
    try {
      const t = localStorage.getItem(THEME_KEY);
      return t === "light" || t === "dark" ? t : null;
    } catch (e) { return null; }        /* Safari private browsing throws on access */
  }

  function currentTheme() {
    const set = document.documentElement.getAttribute("data-theme");
    if (set === "light" || set === "dark") return set;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }

  function applyTheme() {
    const theme = currentTheme();
    document.querySelectorAll("[data-theme-toggle]").forEach(b => {
      /* Show the destination, not the current state — same idea as [data-eye] showing an
         open eye while the balance is hidden. */
      b.textContent = theme === "light" ? "🌙" : "☀️";
      b.setAttribute("aria-label", theme === "light" ? "Switch to dark theme" : "Switch to light theme");
      b.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    });
    document.dispatchEvent(new CustomEvent("sb:themechange", { detail: { theme } }));
  }

  function toggleTheme() {
    const next = currentTheme() === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* storage disabled */ }
    applyTheme();
  }

  /* Charts do not need redrawing — their paint is emitted as live var() so the browser
     re-resolves it. This listener only exists so the glyph stays correct when the OS theme
     changes underneath a user who has not pinned a preference. */
  function watchSystemTheme() {
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => { if (!storedTheme()) applyTheme(); };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);     /* older Safari */
  }

  /* --------------------------------------------------------- notifications */
  let lastNotificationId = null;

  async function pollNotifications() {
    try {
      const d = await getJSON("/api/notifications/recent?limit=6");
      const items = d.notifications || [];
      if (lastNotificationId === null) {
        lastNotificationId = items.length ? items[0].id : "";
        updateBadge(d.unread);
        return;
      }
      const fresh = [];
      for (const n of items) {
        if (n.id === lastNotificationId) break;
        fresh.push(n);
      }
      if (items.length) lastNotificationId = items[0].id;
      fresh.reverse().forEach(n => toast(n.subject, n.preview, n.severity || "info"));
      updateBadge(d.unread);
    } catch (_) { /* polling must never break the page */ }
  }

  function updateBadge(count) {
    const b = document.getElementById("notif-badge");
    if (!b) return;
    if (count > 0) { b.textContent = count > 99 ? "99+" : count; b.classList.remove("hidden"); }
    else b.classList.add("hidden");
  }

  /* ---------------------------------------------------------------- utils */
  function escapeHtml(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* Mirrors core.money.fmt() for the handful of figures the client formats itself. Reads
     the same table Python declared, handed over as window.SB_CURRENCY by base.html. */
  function money(n, currency, opts) {
    opts = opts || {};
    const c = String(currency || "").toUpperCase();
    const dp = opts.dp === undefined ? (c === "JPY" ? 0 : 2) : opts.dp;
    const body = Number(n).toLocaleString(undefined,
      { minimumFractionDigits: dp, maximumFractionDigits: dp });
    if (!c) return body;
    const sym = (window.SB_CURRENCY || {})[c] || c;
    const out = /^[A-Z]{2,}$/.test(sym) ? `${sym} ${body}` : `${sym}${body}`;
    return (opts.code === false || sym === c) ? out : `${out} ${c}`;
  }

  /* --------------------------------------------------- post-login fraud prompt
     A pending alert used to reach the customer as a 6-second toast they had to already
     be looking at, plus a page they had to navigate to. If the bank has frozen your card
     it should be the first thing you are told, and it should be answerable on the spot --
     so this opens the moment they sign in, and both answers post to the SAME endpoint the
     Alerts page uses. No decision logic is duplicated here. */

  function alertPromptHtml(a) {
    const reasons = (a.reasons || []).filter(r => r.plain).slice(0, 4);
    const held = a.held_amount
      ? `<div class="note note-warn" style="margin-top:.6rem">
           <strong>${escapeHtml(a.held_amount)} to ${escapeHtml(a.held_to)} is on hold.</strong>
           <div class="tiny">The money has not left your account. Confirming below releases it.</div>
         </div>`
      : "";
    return `
      <div class="note note-danger">
        <strong>${escapeHtml(a.summary || "We stopped a transaction on your account")}</strong>
        <div class="tiny" style="margin-top:.3rem">
          Risk ${a.risk_score}/100 (${escapeHtml(a.risk_level || "")}) ·
          ${escapeHtml(a.action || "")} · raised ${escapeHtml(a.raised || "")}
        </div>
      </div>
      <dl class="kv" style="margin-top:.7rem">
        <dt>Amount</dt><dd>${escapeHtml(a.amount || "")}</dd>
        <dt>Merchant</dt><dd>${escapeHtml(a.merchant || "")}</dd>
        <dt>Where</dt><dd>${escapeHtml(a.location || "")}</dd>
        <dt>How</dt><dd>${escapeHtml(a.channel || "")}</dd>
      </dl>
      ${reasons.length ? `<div class="label" style="margin-top:.8rem">Why we stopped it</div>
        <ul class="tiny">${reasons.map(r =>
          `<li><strong>${escapeHtml(r.title || "")}</strong> — ${escapeHtml(r.plain)}</li>`).join("")}</ul>` : ""}
      ${held}
      <div class="row" style="margin-top:1rem;gap:.5rem;flex-wrap:wrap">
        <button class="btn btn-ok"    data-verdict="genuine" data-alert="${escapeHtml(a.alert_id)}">
          Yes — that was me</button>
        <button class="btn btn-danger" data-verdict="fraud"  data-alert="${escapeHtml(a.alert_id)}">
          No — freeze my card</button>
        <button class="btn btn-ghost" data-ask-alert>Ask the assistant</button>
      </div>
      <div class="tiny dim" style="margin-top:.6rem">
        You can also answer this later on your Alerts page.
      </div>`;
  }

  function wireAlertPrompt(alerts) {
    let i = 0;
    function show() {
      const a = alerts[i];
      if (!a) { closeModal(); return; }
      openModal(alertPromptHtml(a), {
        title: alerts.length > 1
          ? `Please check this — ${i + 1} of ${alerts.length}`
          : "Please check this transaction",
        subtitle: "We stopped it and need you to confirm before we do anything else",
      });
      const host = document.getElementById("modal-host");
      host.querySelectorAll("[data-verdict]").forEach(btn => {
        btn.addEventListener("click", async () => {
          host.querySelectorAll("button").forEach(b => b.disabled = true);
          /* A form post, not fetch: `respond_alert` redirects and flashes, and letting
             the browser follow that keeps one code path for both entry points. */
          const f = document.createElement("form");
          f.method = "post";
          f.action = `/alerts/${encodeURIComponent(btn.dataset.alert)}/respond`;
          f.innerHTML = `<input type="hidden" name="verdict" value="${btn.dataset.verdict}">`;
          document.body.appendChild(f);
          f.submit();
        });
      });
      const ask = host.querySelector("[data-ask-alert]");
      if (ask) ask.addEventListener("click", () => {
        closeModal();
        if (window.SBChat) SBChat.ask("Why was this transaction stopped?");
      });
    }
    show();
  }

  async function checkPendingAlerts() {
    try {
      const d = await getJSON("/api/alerts/pending");
      if (d.just_signed_in && (d.alerts || []).length) wireAlertPrompt(d.alerts);
    } catch (_) { /* never block a page load over this */ }
  }

  /* ------------------------------------------------------- password change form
     Opened by the assistant via `ui_action`, but the password itself goes straight from
     this form to /account/password. It is never a chat message, so it never reaches a
     prompt, the conversation history, or the recorded response cache. */

  function passwordFormHtml() {
    return `
      <form id="pw-form" autocomplete="off">
        <div class="note note-ok tiny">
          We never see this in chat — it posts straight to your account.
        </div>
        <label class="label" style="margin-top:.8rem" for="pw-cur">Current password</label>
        <input id="pw-cur" type="password" name="current_password" autocomplete="current-password">
        <label class="label" style="margin-top:.6rem" for="pw-new">New password</label>
        <input id="pw-new" type="password" name="new_password" autocomplete="new-password" required>
        <label class="label" style="margin-top:.6rem" for="pw-cnf">Confirm new password</label>
        <input id="pw-cnf" type="password" name="confirm_password" autocomplete="new-password" required>
        <div class="row" style="margin-top:.6rem;gap:.5rem">
          <button class="btn btn-sm btn-ghost" type="button" data-suggest>Suggest a strong one</button>
          <span class="tiny dim" data-suggestion></span>
        </div>
        <div class="note note-warn tiny hidden" data-pw-error style="margin-top:.7rem"></div>
        <div class="row" style="margin-top:1rem;gap:.5rem">
          <button class="btn btn-primary" type="submit">Update password</button>
          <button class="btn btn-ghost" type="button" data-close>Cancel</button>
        </div>
      </form>`;
  }

  function openPasswordForm() {
    openModal(passwordFormHtml(), {
      title: "Change your password",
      subtitle: "Typed here, not in the chat",
    });
    const host = document.getElementById("modal-host");
    const form = host.querySelector("#pw-form");
    const err = host.querySelector("[data-pw-error]");

    host.querySelector("[data-suggest]").addEventListener("click", async () => {
      try {
        const d = await getJSON("/api/account/passphrase");
        host.querySelector("[data-suggestion]").textContent = d.passphrase || "";
      } catch (_) { /* suggestion is a convenience, never a blocker */ }
    });
    form.querySelectorAll("[data-close]").forEach(b =>
      b.addEventListener("click", closeModal));

    form.addEventListener("submit", async e => {
      e.preventDefault();
      err.classList.add("hidden");
      const body = Object.fromEntries(new FormData(form).entries());
      try {
        const d = await postJSON("/account/password", body);
        closeModal();
        toast("Password updated", d.message || "", "ok");
      } catch (ex) {
        err.textContent = ex.message || "That didn't work.";
        err.classList.remove("hidden");
      }
    });
  }

  /* ------------------------------------------------------------------ init */
  document.addEventListener("DOMContentLoaded", () => {
    wireDrilldowns();
    applyMask();
    applyTheme();
    watchSystemTheme();
    document.querySelectorAll("[data-eye]").forEach(b => b.addEventListener("click", toggleMask));
    document.querySelectorAll("[data-theme-toggle]").forEach(b => b.addEventListener("click", toggleTheme));
    if (document.body.dataset.poll === "1") {
      pollNotifications();
      setInterval(pollNotifications, 5000);
    }
    if (document.body.dataset.alertPopup === "1") checkPendingAlerts();
  });

  global.SB = {
    getJSON, postJSON, toast, openModal, closeModal, loadingModal,
    drill, wireDrilldowns, escapeHtml, money, prettyKey, renderDrill,
    pollNotifications, applyMask, applyTheme, toggleTheme, currentTheme,
    paintCharts, chartSpecs, chartSlot, checkPendingAlerts,
  };

  /* The chat widget calls this when a tool returns ui_action: "password_form". */
  global.SBAccount = { openPasswordForm };
})(window);
