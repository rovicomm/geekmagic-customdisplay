// ClockDisplay: Claude usage section for the neosoft Zebar bar.
// Installed into the widget pack by `clock zebar install`; reads the ClockDisplay tray app's
// localhost server. It sits in the right-hand group, between the volume/media block and the
// weather/date. Hidden while the tray app isn't running or its "Zebar bar" toggle is off.
(() => {
  const PORT = Number(document.currentScript?.dataset.port) || 47815;
  const BASE = `http://127.0.0.1:${PORT}`;
  const POLL_MS = 5000;
  const RING = 2 * Math.PI * 42;

  const el = document.createElement("div");
  el.id = "cd-claude";
  el.hidden = true;
  el.innerHTML = `
    <svg class="cd-spark" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.5l1.6 6.1 5.1-3.7-3.7 5.1 6.1 1.6-6.1 1.6 3.7 5.1-5.1-3.7L12 20.6l-1.6-6.1-5.1 3.7 3.7-5.1L2.9 12l6.1-1.6-3.7-5.1 5.1 3.7z"/></svg>
    <div class="cd-item cd-five">
      <span class="cd-label">5h</span>
      <div class="cd-line">
        <img class="cd-fire" src="${BASE}/fire.png" alt="" hidden>
        <div class="cd-fill"></div>
      </div>
      <span class="cd-pct">–</span>
    </div>
    <div class="cd-item cd-week">
      <div class="cd-gauge">
        <svg viewBox="0 0 100 100"><circle class="cd-track" r="42" cx="50" cy="50"/>
          <circle class="cd-arc" r="42" cx="50" cy="50" stroke-dasharray="${RING}" stroke-dashoffset="${RING}" transform="rotate(-90 50 50)"/></svg>
        <span class="cd-ring-label">7d</span>
      </div>
    </div>`;

  // neosoft: right before the weather (or the dot ahead of the date when there's no weather
  // yet). Svelte inserts the weather in front of its own anchor, which may land it before us,
  // so this runs again on every DOM change; it only moves our node, never the bar's.
  // Nothing is touched until the app is live (the date has text): SvelteKit hydrates the
  // prerendered markup in place, and an extra node in it makes hydration fail.
  function neosoftSpot() {
    const dot = document.querySelector(".tabler-icon-point-filled.mr-2");
    const row = dot?.parentElement;
    if (!row || !row.querySelector("p.whitespace-nowrap")?.textContent.trim()) return null;
    return { kind: "neosoft", row, before: row.querySelector(":scope > .truncate") || dot };
  }

  // Any other bar: the start of the right-hand group. Probe leftwards from the right edge
  // (bars often have padding there) for the first item, then take the outermost flex row
  // around it that is narrower than most of the bar: the group, not one item inside it.
  // Only tried once the page has been quiet for a while, so the bar has finished rendering.
  function fallbackSpot() {
    const y = window.innerHeight / 2;
    for (let x = window.innerWidth - 4; x > window.innerWidth * 0.6; x -= 8) {
      let group = null;
      for (let n = document.elementFromPoint(x, y); n && n !== document.body; n = n.parentElement) {
        if (n === el || el.contains(n)) continue;
        const cs = getComputedStyle(n);
        if (cs.display.includes("flex") && !cs.flexDirection.startsWith("column")
            && n.getBoundingClientRect().width < window.innerWidth * 0.6) group = n;
      }
      const first = group && [...group.children].find((c) => c !== el);
      if (first) return { kind: "fallback", row: group, before: first };
    }
    return null;
  }

  let quiet = false;
  let quietTimer = 0;
  function place() {
    const spot = neosoftSpot() || (quiet && fallbackSpot());
    if (!spot) return;
    el.dataset.cdAnchor = spot.kind;
    if (el.parentElement !== spot.row || el.nextElementSibling !== spot.before) spot.row.insertBefore(el, spot.before);
  }
  const settle = () => { quiet = true; place(); };
  new MutationObserver((records) => {
    if (records.every((r) => r.target === el || el.contains(r.target))) return; // our own updates
    if (!quiet) {
      clearTimeout(quietTimer);
      quietTimer = setTimeout(settle, 3000);
    }
    place();
  }).observe(document.body, { childList: true, subtree: true });
  quietTimer = setTimeout(settle, 3000);
  setTimeout(settle, 10000); // bars that redraw every second or two are never quiet for 3s
  place();

  // 5h: a bar like the device card's, with flames rising off the filled part when on fire.
  function setLine(pct, color, onFire) {
    const item = el.querySelector(".cd-five");
    const p = Math.max(0, Math.min(pct, 100));
    item.style.setProperty("--cd-level", onFire ? "#e64614" : color);
    item.querySelector(".cd-fill").style.width = `${p}%`;
    const fire = item.querySelector(".cd-fire");
    fire.hidden = !onFire;
    fire.style.width = `${Math.max(p, 25)}%`; // like the device: never just a sliver of flame
    item.querySelector(".cd-pct").textContent = `${Math.round(Math.max(0, pct))}%`;
  }

  // 7d: a ring like the bar's CPU/memory gauges, labelled inside; the % is in the tooltip.
  function setRing(pct, color) {
    const item = el.querySelector(".cd-week");
    const p = Math.max(0, Math.min(pct, 100));
    item.querySelector(".cd-arc").style.strokeDashoffset = RING * (1 - p / 100);
    item.style.setProperty("--cd-level", color);
  }

  let lastAnchor = "";
  function reportAnchor() {
    if (el.hidden || !el.isConnected) return;
    const r = el.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const a = {
      x: Math.round((window.screenX + r.left) * dpr), y: Math.round((window.screenY + r.top) * dpr),
      w: Math.round(r.width * dpr), h: Math.round(r.height * dpr),
      primary: window.screenX === 0 && window.screenY === 0,
    };
    const key = JSON.stringify(a);
    if (key === lastAnchor) return;
    lastAnchor = key;
    fetch(`${BASE}/anchor`, { method: "POST", headers: { "Content-Type": "application/json" }, body: key })
      .catch(() => { lastAnchor = ""; });
  }
  window.addEventListener("resize", () => { lastAnchor = ""; reportAnchor(); });

  async function poll() {
    try {
      const res = await fetch(`${BASE}/usage?t=${Date.now()}`, { cache: "no-store" }); // Zebar caches by URL
      const u = await res.json();
      el.hidden = !u.enabled || u.five_pct === undefined;
      if (!el.hidden) {
        setLine(u.five_pct, u.color5, !!u.on_fire);
        setRing(u.week_pct, u.color7);
        el.classList.toggle("cd-on-fire", !!u.on_fire);
        el.classList.toggle("cd-stale", !!u.error);
        el.title = `Claude 5h ${Math.round(u.five_pct)}% · resets ${u.five_reset}\n` +
                   `7d ${Math.round(u.week_pct)}% · resets ${u.week_reset}` +
                   (u.burn_rate != null ? `\nburning ${Math.round(u.burn_rate)}%/h` : "") +
                   (u.error ? `\n${u.error}` : "");
        place();
        if (u.need_anchor) lastAnchor = ""; // the tray app restarted: tell it again
        reportAnchor();
      }
    } catch {
      el.hidden = true; // tray app not running
    }
    setTimeout(poll, POLL_MS);
  }
  poll();
})();
