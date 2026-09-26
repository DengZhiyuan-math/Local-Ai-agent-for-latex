/* PDF.js viewer shared by the editor's inline pane and the pop-out tab.
   Expects #pdf-scroll > #pdf-pages, #pdf-empty, #zoom-in/-out/-fit, #zoom-label, #page-label. */
"use strict";

pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdf.worker.js";

// Pages within RENDER_PX of the visible area are drawn ahead of scrolling. Pages farther
// than KEEP_PX give up their canvas, so a long paper does not hold hundreds of MB of them.
const RENDER_PX = 600, KEEP_PX = 2400;
const PAGE_PAD = 12, PAGE_GAP = 12;          // #pdf-pages padding and gap (app.css)

const PV = {
  pdf: null, mtime: null, scale: 0, eff: 1, views: [], scaleKey: "scale",
  onInverse: null,              // ({page, x, y}) in PDF points from the top-left
  observer: null, keeper: null, seq: 0, gen: 0,

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
    sc.addEventListener("dblclick", (e) => {
      const div = e.target.closest(".pdf-page"); if (!div || !this.onInverse) return;
      const rect = div.getBoundingClientRect();
      this.onInverse({ page: +div.dataset.page, x: (e.clientX - rect.left) / this.eff, y: (e.clientY - rect.top) / this.eff });
    });
  },

  async load(mtime) {
    const seq = ++this.seq;
    let data;
    try {
      const res = await fetch("/pdf?t=" + mtime);
      if (!res.ok) return;
      data = new Uint8Array(await res.arrayBuffer());
    } catch { return; }
    const doc = await pdfjsLib.getDocument({ data }).promise.catch(() => null);
    if (!doc) return;
    if (seq !== this.seq) { doc.destroy(); return; }       // a newer build's PDF is on its way
    const old = this.pdf;
    this.pdf = doc; this.mtime = mtime;
    await this.layout(true);
    if (old) old.destroy();
  },

  // Lay out the pages of the current document at the current zoom. The pages that will be
  // on screen are drawn before the new layout replaces the old one, so a rebuild or a zoom
  // never shows blank pages.
  async layout(keepScroll) {
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
    const ratio = () => (keepScroll && sc.scrollHeight > 0 ? sc.scrollTop / sc.scrollHeight : 0);
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
    return pv.done;
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
