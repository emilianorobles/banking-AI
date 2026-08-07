/* SentinelBank front-end runtime: fetch helpers, toasts, modals, drill-downs,
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
  /* Every health and security card carries data-drill="<kind>:<key>". Clicking it asks
     the server what actually went into that number and renders the working. This is the
     whole reason the migration happened, so it is deliberately generic and reusable. */
  function renderDrill(d) {
    let html = "";

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

  function money(n, currency) {
    return Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 }) +
           (currency ? " " + currency : "");
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
  });

  global.SB = {
    getJSON, postJSON, toast, openModal, closeModal, loadingModal,
    drill, wireDrilldowns, escapeHtml, money, prettyKey, renderDrill,
    pollNotifications, applyMask, applyTheme, toggleTheme, currentTheme,
  };
})(window);
