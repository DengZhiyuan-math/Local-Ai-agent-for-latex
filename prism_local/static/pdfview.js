/* PDF.js viewer shared by the editor's inline pane and the pop-out tab.
   Expects #pdf-scroll > #pdf-pages, #pdf-empty, #zoom-in/-out/-fit, #zoom-label, #page-label. */
"use strict";

pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdf.worker.js";

// Pages within RENDER_PX of the visible area are drawn ahead of scrolling. Pages farther
// than KEEP_PX give up their canvas, so a long paper does not hold hundreds of MB of them.
const RENDER_PX = 600, KEEP_PX = 2400;
const PAGE_PAD = 12, PAGE_GAP = 12;          // #pdf-pages padding and gap (app.css)

const PV = {
  pdf: null, mtime: null, main: null, scale: 0, eff: 1, views: [], scaleKey: "scale",
  onInverse: null,              // ({page, x, y}) in PDF points from the top-left
  observer: null, keeper: null, seq: 0, gen: 0,
  back: [],                     // scroll positions to return to after following a link
  spots: {},                    // document -> where it was scrolled to (a fraction), for switching back
  text: new Map(),              // page index -> Promise of its text content (current PDF)
  find: { q: "", hits: [], cur: -1, index: null },   // search in the PDF
  here: false,                  // the PDF was clicked last (⌘F searches the PDF, not the editor)

  init(opts) {
    Object.assign(this, opts);
    this.scale = store.get(this.scaleKey, 0);
    const sc = $("#pdf-scroll");
    sc.addEventListener("scroll", () => this.updatePageLabel());
    $("#zoom-in").onclick = () => this.setZoom(Math.min(4, this.eff * 1.15));
    $("#zoom-out").onclick = () => this.setZoom(Math.max(0.3, this.eff / 1.15));
    $("#zoom-fit").onclick = () => this.setZoom(0);
    let t = null;
    new ResizeObserver(() => { if (!this.scale) { clearTimeout(t); t = setTimeout(() => this.layout(true), 150); } }).observe(sc);
    // Double-click, or Ctrl/⌘-click, jumps to the source.
    const inverse = (e) => {
      const div = e.target.closest(".pdf-page"); if (!div || !this.onInverse) return;
      e.preventDefault();
      if (e.type === "dblclick") window.getSelection()?.removeAllRanges();   // not the word it selected
      const rect = div.getBoundingClientRect();
      this.onInverse({ page: +div.dataset.page, x: (e.clientX - rect.left) / this.eff, y: (e.clientY - rect.top) / this.eff });
    };
    sc.addEventListener("dblclick", (e) => { if (!e.target.closest(".pdf-link")) inverse(e); });
    sc.addEventListener("click", (e) => {
      if (e.ctrlKey || e.metaKey) { e.preventDefault(); return inverse(e); }
      const a = e.target.closest(".pdf-link");
      if (a && a.dataset.dest !== undefined) { e.preventDefault(); this.follow(JSON.parse(a.dataset.dest)); }
    });
    // Back after following a link: the button, Alt+←, or the mouse's back button.
    const back = document.createElement("button");
    back.id = "pdf-back"; back.className = "icon"; back.hidden = true;
    back.title = "Back to where you followed the link (Alt+←)"; back.setAttribute("aria-label", "Back");
    back.innerHTML = icon("arrow").replace("<svg", '<svg style="transform: scaleX(-1)"');
    back.onclick = () => this.goBack();
    $("#zoom-fit").after(back);
    window.addEventListener("keydown", (e) => { if (e.altKey && e.key === "ArrowLeft" && this.back.length) { e.preventDefault(); this.goBack(); } });
    sc.addEventListener("mouseup", (e) => { if (e.button === 3 && this.back.length) { e.preventDefault(); this.goBack(); } });
    this.initOutline(back);
    this.initFind();
  },

  // A toolbar button made here, so that both PDF views (editor pane, pop-out tab) get it.
  button(id, ic, title, onclick) {
    const b = document.createElement("button");
    b.id = id; b.className = "icon"; b.title = title; b.setAttribute("aria-label", title);
    b.innerHTML = icon(ic); b.onclick = onclick;
    return b;
  },

  /* ---------------------------------------------------------------- bookmarks */
  initOutline(after) {
    const btn = this.button("pdf-outline-btn", "list", "Bookmarks (the PDF's table of contents)", () => this.toggleOutline());
    after.after(btn);
    const panel = document.createElement("nav");
    panel.id = "pdf-outline"; panel.hidden = true;
    $("#pdf-pane").appendChild(panel);
    panel.addEventListener("click", (e) => {
      const tw = e.target.closest(".tw");
      if (tw) { tw.closest("li").classList.toggle("shut"); return; }
      const it = e.target.closest(".ol-item"); if (!it) return;
      if (it.dataset.url) window.open(it.dataset.url, "_blank", "noopener");
      else if (it.dataset.dest) this.follow(JSON.parse(it.dataset.dest));
    });
  },

  async toggleOutline(open = $("#pdf-outline").hidden) {
    $("#pdf-outline").hidden = !open;
    $("#pdf-outline-btn").classList.toggle("on", open);
    if (open) await this.drawOutline();
  },

  async drawOutline() {
    const panel = $("#pdf-outline"), pdf = this.pdf;
    if (!pdf) { panel.innerHTML = `<p class="ol-none">No PDF yet.</p>`; return; }
    const outline = await pdf.getOutline().catch(() => null);
    if (pdf !== this.pdf) return;
    if (!outline || !outline.length) {
      panel.innerHTML = `<p class="ol-none">This PDF has no bookmarks. With <code>\\usepackage{hyperref}</code>, LaTeX makes them from the sections.</p>`;
      return;
    }
    // Top-level entries open, deeper ones closed (▸ opens them).
    const list = (items, depth) => "<ul>" + items.map((it) => {
      const kids = it.items && it.items.length;
      const data = it.url ? `data-url="${esc(it.url)}"` : it.dest ? `data-dest="${esc(JSON.stringify(it.dest))}"` : "";
      return `<li class="${kids && depth >= 1 ? "shut" : ""}"><div class="ol-row" style="padding-left:${4 + depth * 14}px">`
        + `<span class="tw">${kids ? "▾" : ""}</span><a class="ol-item" ${data} title="${esc(it.title)}">${esc(it.title)}</a></div>`
        + (kids ? list(it.items, depth + 1) : "") + "</li>";
    }).join("") + "</ul>";
    panel.innerHTML = list(outline, 0);
  },

  /* ---------------------------------------------------------------- search */
  initFind() {
    const btn = this.button("pdf-find-btn", "search", keys("Search in the PDF (⌘F while the PDF is active)"), () => this.openFind());
    $("#pdf-outline-btn").after(btn);
    const bar = document.createElement("div");
    bar.id = "pdf-find"; bar.hidden = true;
    bar.innerHTML = `<input type="search" placeholder="Search the PDF" spellcheck="false" aria-label="Search the PDF">
      <span class="count"></span>
      <button class="icon tiny" data-step="-1" title="Previous (Shift+Enter)" aria-label="Previous">${icon("up")}</button>
      <button class="icon tiny" data-step="1" title="Next (Enter)" aria-label="Next">${icon("down")}</button>
      <button class="icon tiny" data-close="1" title="Close (Esc)" aria-label="Close">×</button>`;
    $("#pdf-pane").appendChild(bar);
    const input = bar.querySelector("input");
    let t = null;
    input.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => this.search(input.value), 200); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); clearTimeout(t); this.search(input.value, e.shiftKey ? -1 : 1); }
      else if (e.key === "Escape") { e.preventDefault(); this.closeFind(); }
    });
    bar.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      if (b.dataset.close) this.closeFind(); else this.search(input.value, +b.dataset.step);
    });
    // ⌘F / Ctrl+F searches the PDF when it was clicked last (in the pop-out tab, always);
    // otherwise it stays the editor's search.
    $("#pdf-pane").addEventListener("pointerdown", () => { this.here = true; }, true);
    document.addEventListener("pointerdown", (e) => { if (!e.target.closest("#pdf-pane")) this.here = false; }, true);
    window.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "f" && (this.standalone || this.here)) {
        e.preventDefault(); e.stopPropagation(); this.openFind();
      }
    }, true);
  },

  openFind() {
    const bar = $("#pdf-find"), input = bar.querySelector("input");
    bar.hidden = false;
    const sel = String(window.getSelection() || "").trim();
    if (sel && sel.length < 100 && !sel.includes("\n")) input.value = sel;
    input.focus(); input.select();
    if (input.value) this.search(input.value);
  },

  closeFind() {
    $("#pdf-find").hidden = true;
    Object.assign(this.find, { q: "", hits: [], cur: -1 });
    $("#pdf-find .count").textContent = "";
    this.drawHits();
  },

  textContent(idx) {
    if (!this.text.has(idx)) this.text.set(idx, this.pdf.getPage(idx + 1).then((p) => p.getTextContent()));
    return this.text.get(idx);
  },

  // Each page's text as one searchable string, and for each of its characters the text
  // item and character it came from. Case and ligatures (ﬁ = fi) do not matter.
  findIndex() {
    const pdf = this.pdf;
    if (this.find.index && this.find.index.pdf === pdf) return this.find.index.pages;
    const pages = Promise.all(Array.from({ length: pdf.numPages }, (_, idx) => this.textContent(idx).then((tc) => {
      let s = "";
      const map = [];
      tc.items.forEach((it, i) => {
        for (let k = 0; k < (it.str || "").length; k++) {
          for (const ch of it.str[k].normalize("NFKC").toLowerCase()) {
            if (/\s/.test(ch)) { if (s && !s.endsWith(" ")) { s += " "; map.push(null); } }
            else { s += ch; map.push([i, k]); }
          }
        }
        if (it.hasEOL && s && !s.endsWith(" ")) { s += " "; map.push(null); }
      });
      return { s, map, items: tc.items };
    })));
    this.find.index = { pdf, pages };
    return pages;
  },

  // Find `q`. step 0: a new query, from the page you are reading; ±1: next/previous match.
  async search(q, step = 0) {
    const f = this.find, count = $("#pdf-find .count");
    const norm = (x) => x.normalize("NFKC").toLowerCase().replace(/\s+/g, " ").trim();
    const want = norm(q);
    if (!want || !this.pdf) { Object.assign(f, { q: "", hits: [], cur: -1 }); count.textContent = ""; this.drawHits(); return; }
    if (want !== f.q) {
      count.textContent = "…";
      const pages = await this.findIndex();
      if (norm($("#pdf-find input").value) !== want) return;            // typed on meanwhile
      const hits = [];
      pages.forEach((p, idx) => {
        for (let i = p.s.indexOf(want); i >= 0 && hits.length < 5000; i = p.s.indexOf(want, i + 1))
          hits.push({ idx, start: i, end: i + want.length });
      });
      const page = (parseInt($("#page-label").textContent, 10) || 1) - 1;
      const first = hits.findIndex((h) => h.idx >= page);
      Object.assign(f, { q: want, hits, cur: hits.length ? (first < 0 ? 0 : first) : -1 });
      step = 0;
    }
    if (!f.hits.length) { count.textContent = "no matches"; this.drawHits(); return; }
    f.cur = (f.cur + step + f.hits.length) % f.hits.length;
    count.textContent = `${f.cur + 1} / ${f.hits.length}${f.hits.length >= 5000 ? "+" : ""}`;
    await this.drawHits();
    const cur = document.querySelector(".pdf-hit.cur");
    if (cur) {
      const sc = $("#pdf-scroll");
      sc.scrollTop = cur.closest(".pdf-page").offsetTop + cur.offsetTop - sc.clientHeight / 3;
    }
  },

  // Boxes over the matches. Where the page's text layer is drawn they are measured on its
  // text; elsewhere they are placed from the text items, in proportion to the characters.
  async drawHits() {
    document.querySelectorAll(".pdf-hit").forEach((el) => el.remove());
    const f = this.find;
    if (!f.hits.length || !f.index || f.index.pdf !== this.pdf) return;
    const pages = await f.index.pages;
    document.querySelectorAll(".pdf-hit").forEach((el) => el.remove());
    f.hits.forEach((h, n) => {
      const pv = this.views[h.idx]; if (!pv) return;
      const { map, items } = pages[h.idx];
      const spans = new Map();                       // item -> [first char, last char]
      for (let i = h.start; i < h.end; i++) {
        const m = map[i]; if (!m) continue;
        const r = spans.get(m[0]);
        if (!r) spans.set(m[0], [m[1], m[1]]); else r[1] = m[1];
      }
      const box = pv.div.getBoundingClientRect();
      for (const [i, [k0, k1]] of spans) {
        const node = pv.textDivs && pv.textDivs[i] && pv.textDivs[i].firstChild;
        if (node && node.nodeType === 3 && pv.textDivs[i].isConnected && k1 < node.length) {
          const r = document.createRange();
          r.setStart(node, k0); r.setEnd(node, k1 + 1);
          for (const c of r.getClientRects()) {
            const el = document.createElement("div");
            el.className = "pdf-hit" + (n === f.cur ? " cur" : "");
            Object.assign(el.style, { left: c.left - box.left + "px", top: c.top - box.top + "px", width: c.width + "px", height: c.height + "px" });
            pv.div.appendChild(el);
          }
          continue;
        }
        const it = items[i], len = it.str.length || 1;
        const tx = pdfjsLib.Util.transform(pv.vp.transform, it.transform);
        const fh = Math.hypot(tx[2], tx[3]), w = it.width * pv.vp.scale;
        const el = document.createElement("div");
        el.className = "pdf-hit" + (n === f.cur ? " cur" : "");
        Object.assign(el.style, { left: tx[4] + (w * k0) / len + "px", top: tx[5] - fh + "px",
          width: Math.max(2, (w * (k1 - k0 + 1)) / len) + "px", height: fh * 1.25 + "px" });
        pv.div.appendChild(el);
      }
    });
  },

  // The page's text, invisible over the canvas, so that it can be selected and copied.
  async textLayer(pv) {
    const tc = await this.textContent(+pv.div.dataset.page - 1).catch(() => null);
    if (!tc) return;
    const div = document.createElement("div"), textDivs = [];   // one span per text item
    div.className = "textLayer";
    div.style.setProperty("--scale-factor", pv.vp.scale);
    pv.div.appendChild(div);
    await pdfjsLib.renderTextLayer({ textContentSource: tc, container: div, viewport: pv.vp, textDivs })
      .promise.catch(() => {});
    pv.textDivs = textDivs;
    if (this.find.hits.some((h) => this.views[h.idx] === pv)) this.drawHits();   // measure them now
  },

  async load(mtime, main) {
    const seq = ++this.seq;
    const switched = main !== this.main;
    if (switched || !mtime) {
      const old = this.pdf, sc = $("#pdf-scroll");
      if (old && this.main && sc.scrollHeight) this.spots[this.main] = sc.scrollTop / sc.scrollHeight;
      this.pdf = null; this.mtime = null; this.main = main; this.gen++;
      this.observer?.disconnect(); this.keeper?.disconnect();
      this.views.forEach((v) => this.unrender(v)); this.views = [];
      this.text = new Map(); Object.assign(this.find, { hits: [], cur: -1, index: null }); this.back = [];
      $("#pdf-pages").replaceChildren(); $("#pdf-empty").hidden = false;
      $("#pdf-scroll").scrollTop = 0; $("#page-label").textContent = "";
      $("#pdf-outline").replaceChildren(); $("#pdf-find .count").textContent = ""; $("#pdf-back").hidden = true;
      if (old) old.destroy();
    }
    if (!mtime) return;
    let data;
    try {
      const res = await fetch("/pdf?t=" + mtime + "&main=" + encodeURIComponent(main || ""));
      if (!res.ok) return;
      data = new Uint8Array(await res.arrayBuffer());
    } catch { return; }
    // disableFontFace: draw glyphs as paths. Through the browser's fonts, symbols of TeX's
    // Type 1 fonts (the minus and the equals sign of CMSY/CMR) go missing.
    const doc = await pdfjsLib.getDocument({ data, disableFontFace: true,
      cMapUrl: "/static/vendor/cmaps/", cMapPacked: true }).promise.catch(() => null);
    if (!doc) return;
    if (seq !== this.seq) { doc.destroy(); return; }       // a newer build's PDF is on its way
    const old = this.pdf;
    this.pdf = doc; this.mtime = mtime;
    this.text = new Map(); this.find.index = null; this.find.hits = [];
    await this.layout(true, switched ? this.spots[main] : undefined);
    if (old) old.destroy();
    if (!$("#pdf-outline").hidden) this.drawOutline();
    if (this.find.q && !$("#pdf-find").hidden) this.search($("#pdf-find input").value);
  },

  // Lay out the pages of the current document at the current zoom. The pages that will be
  // on screen are drawn before the new layout replaces the old one, so a rebuild or a zoom
  // never shows blank pages.
  async layout(keepScroll, at) {
    if (!this.pdf) return;
    const sc = $("#pdf-scroll");
    if (!sc.clientWidth) return;                 // hidden (e.g. popped out)
    const pdf = this.pdf, gen = ++this.gen;
    const pages = await Promise.all(Array.from({ length: pdf.numPages }, (_, i) => pdf.getPage(i + 1)))
      .catch(() => null);                        // the document was destroyed meanwhile
    if (!pages || pdf !== this.pdf || gen !== this.gen) return;   // or replaced, or laid out again
    const fit = Math.max(0.3, Math.min(4, (sc.clientWidth - 28) / pages[0].getViewport({ scale: 1 }).width));
    const scale = this.scale || fit;
    const wrap = document.createElement("div");
    wrap.id = "pdf-pages";
    const views = pages.map((page, idx) => {
      const vp = page.getViewport({ scale });
      const div = document.createElement("div");
      div.className = "pdf-page"; div.style.width = vp.width + "px"; div.style.height = vp.height + "px";
      div.dataset.page = idx + 1;
      wrap.appendChild(div);
      return { page, vp, div, rendered: false, task: null, canvas: null, token: null };
    });
    // at: a fraction to scroll to (a document shown again); otherwise where the view is now.
    const ratio = () => at ?? (keepScroll && sc.scrollHeight > 0 ? sc.scrollTop / sc.scrollHeight : 0);
    // Where the pages will be, by the same arithmetic as the CSS, to draw the visible ones first.
    let y = PAGE_PAD;
    const tops = views.map((v) => { const top = y; y += v.vp.height + PAGE_GAP; return top; });
    const total = y - PAGE_GAP + PAGE_PAD, from = ratio() * total, to = from + sc.clientHeight;
    const drawn = Promise.all(views.filter((v, i) => tops[i] < to && tops[i] + v.vp.height > from).map((v) => this.render(v)));
    // PDF.js draws in animation frames, which a hidden tab does not get: never wait long for them.
    await Promise.race([drawn, new Promise((r) => setTimeout(r, document.hidden ? 0 : 1500))]);
    if (pdf !== this.pdf || gen !== this.gen) { views.forEach((v) => this.unrender(v)); return; }
    const keep = ratio();
    const old = this.views;
    this.views = views; this.eff = scale;
    $("#zoom-label").textContent = Math.round(scale * 100) + "%";
    $("#pdf-pages").replaceWith(wrap);
    $("#pdf-empty").hidden = true;
    sc.scrollTop = keep * sc.scrollHeight;
    old.forEach((v) => this.unrender(v));        // off screen now: free their canvases
    this.observe(sc);
    this.updatePageLabel();
    this.drawHits();                             // the search matches, at the new zoom
  },

  observe(sc) {
    if (this.observer) this.observer.disconnect();
    if (this.keeper) this.keeper.disconnect();
    const view = (e) => this.views[+e.target.dataset.page - 1];
    this.observer = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) this.render(view(e));
    }, { root: sc, rootMargin: `${RENDER_PX}px 0px` });
    this.keeper = new IntersectionObserver((entries) => {
      for (const e of entries) if (!e.isIntersecting && view(e)) this.unrender(view(e));
    }, { root: sc, rootMargin: `${KEEP_PX}px 0px` });
    for (const v of this.views) { this.observer.observe(v.div); this.keeper.observe(v.div); }
  },

  render(pv) {
    if (!pv) return Promise.resolve();
    if (pv.rendered) return pv.done || Promise.resolve();
    pv.rendered = true;
    const token = pv.token = {};
    const dpr = window.devicePixelRatio || 1;
    const canvas = document.createElement("canvas");
    canvas.width = Math.floor(pv.vp.width * dpr); canvas.height = Math.floor(pv.vp.height * dpr);
    canvas.style.width = pv.vp.width + "px"; canvas.style.height = pv.vp.height + "px";
    pv.task = pv.page.render({ canvasContext: canvas.getContext("2d"), viewport: pv.vp, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null });
    pv.done = pv.task.promise.then(() => {
      if (pv.token !== token) return;            // released or redrawn meanwhile
      pv.div.prepend(canvas); pv.canvas = canvas;
    }, () => {}).finally(() => { if (pv.token === token) pv.task = null; });
    if (!pv.linked) { pv.linked = true; this.links(pv); this.textLayer(pv); }
    return pv.done;
  },

  // The page's links (hyperref: refs, citations, the contents, URLs) as boxes over the canvas.
  // A link inside the document jumps there; a URL opens in a new tab.
  async links(pv) {
    const annots = await pv.page.getAnnotations({ intent: "display" }).catch(() => []);
    for (const a of annots) {
      if (a.subtype !== "Link" || !a.rect) continue;
      const [x1, y1, x2, y2] = pv.vp.convertToViewportRectangle(a.rect);
      const el = document.createElement("a");
      el.className = "pdf-link";
      Object.assign(el.style, { left: Math.min(x1, x2) + "px", top: Math.min(y1, y2) + "px",
        width: Math.abs(x2 - x1) + "px", height: Math.abs(y2 - y1) + "px" });
      if (a.url || a.unsafeUrl) {
        el.href = a.url || a.unsafeUrl; el.target = "_blank"; el.rel = "noopener noreferrer";
        el.title = el.href;
      } else if (a.dest) {
        el.href = "#"; el.dataset.dest = JSON.stringify(a.dest);
      } else continue;
      pv.div.appendChild(el);
    }
  },

  // Scroll to a PDF destination: a name, or [page ref, {name: "XYZ"|"FitH"|…}, left, top, …].
  async follow(dest) {
    const pdf = this.pdf; if (!pdf) return;
    const d = typeof dest === "string" ? await pdf.getDestination(dest).catch(() => null) : dest;
    if (!Array.isArray(d) || pdf !== this.pdf) return;
    const idx = typeof d[0] === "object" && d[0] ? await pdf.getPageIndex(d[0]).catch(() => -1) : d[0];
    const pv = this.views[idx]; if (!pv) return;
    const kind = d[1] && d[1].name;
    const top = kind === "XYZ" ? d[3] : kind === "FitH" || kind === "FitBH" ? d[2] : kind === "FitR" ? d[5] : null;
    const y = top == null ? 0 : pv.vp.convertToViewportPoint(0, top)[1];
    const sc = $("#pdf-scroll");
    this.back.push(sc.scrollTop); $("#pdf-back").hidden = false;
    sc.scrollTop = pv.div.offsetTop + Math.max(0, y) - 24;
    const hl = document.createElement("div");       // show where it landed
    hl.className = "sync-hl";
    Object.assign(hl.style, { left: "0px", top: Math.max(0, y - 4) + "px", width: "100%", height: "22px" });
    pv.div.appendChild(hl);
    setTimeout(() => hl.remove(), 2400);
  },

  goBack() {
    if (!this.back.length) return;
    $("#pdf-scroll").scrollTop = this.back.pop();
    $("#pdf-back").hidden = !this.back.length;
  },

  unrender(pv) {
    pv.token = null;
    if (pv.task) { pv.task.cancel(); pv.task = null; }
    if (pv.canvas) { pv.canvas.width = pv.canvas.height = 0; pv.canvas.remove(); pv.canvas = null; }   // frees the pixels at once
    pv.rendered = false; pv.done = null;
  },

  updatePageLabel() {
    if (!this.views.length) return;
    const sc = $("#pdf-scroll"), mid = sc.scrollTop + sc.clientHeight / 3;
    let cur = 1;
    for (const pv of this.views) if (pv.div.offsetTop <= mid) cur = +pv.div.dataset.page;
    $("#page-label").textContent = `${cur} / ${this.views.length}`;
  },

  setZoom(s) { this.scale = s; store.set(this.scaleKey, s); this.layout(true); },

  // Scroll to and flash a SyncTeX box {page, x, y, w, h} (PDF points, top-left origin).
  highlight(r) {
    const pv = this.views[r.page - 1]; if (!pv) return;
    const s = this.eff, sc = $("#pdf-scroll");
    sc.scrollTop = pv.div.offsetTop + r.y * s - sc.clientHeight / 3;
    const hl = document.createElement("div");
    hl.className = "sync-hl";
    Object.assign(hl.style, { left: (r.x - 2) * s + "px", top: (r.y - 2) * s + "px", width: (r.w + 4) * s + "px", height: (r.h + 4) * s + "px" });
    pv.div.appendChild(hl);
    setTimeout(() => hl.remove(), 2400);
  },
};
