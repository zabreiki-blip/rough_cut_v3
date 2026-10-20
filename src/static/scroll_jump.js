// Floating "to top" / "to bottom" jump buttons for long pages.
// Pure helpers are exported for tests; the DOM wiring runs only in a browser.

export const SCROLL_JUMP_THRESHOLD = 200;

export function computeVisibility(scrollTop, scrollHeight, clientHeight, threshold = SCROLL_JUMP_THRESHOLD) {
  const maxScroll = Number(scrollHeight) - Number(clientHeight);
  if (!Number.isFinite(maxScroll) || maxScroll <= 0) {
    return { showTop: false, showBottom: false };
  }
  const top = Math.max(0, Number(scrollTop) || 0);
  return {
    showTop: top > threshold,
    showBottom: maxScroll - top > threshold,
  };
}

function isScrollableStyle(el) {
  const overflowY = window.getComputedStyle(el).overflowY;
  return overflowY === "auto" || overflowY === "scroll" || overflowY === "overlay";
}

// Nearest main container that scrolls on its own; otherwise the document.
// On the project page <main class="main"> has no overflow, so the document scrolls.
function resolveScroller() {
  const candidate = document.querySelector("[data-scroll-container], main");
  if (candidate && isScrollableStyle(candidate) && candidate.scrollHeight > candidate.clientHeight) {
    return { el: candidate, isWindow: false };
  }
  return { el: document.scrollingElement || document.documentElement, isWindow: true };
}

export function initScrollJump(root = document) {
  const wrap = root.getElementById("scroll_jump");
  const btnTop = root.getElementById("btn_scroll_top");
  const btnBottom = root.getElementById("btn_scroll_bottom");
  if (!wrap || !btnTop || !btnBottom) return () => {};

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const behavior = () => (reduceMotion.matches ? "auto" : "smooth");
  let frame = 0;
  let scroller = resolveScroller();
  let scrollTarget = null;

  function bindScrollTarget() {
    if (scrollTarget) scrollTarget.removeEventListener("scroll", schedule);
    scroller = resolveScroller();
    scrollTarget = scroller.isWindow ? window : scroller.el;
    scrollTarget.addEventListener("scroll", schedule, { passive: true });
  }

  function update() {
    frame = 0;
    const previous = scroller;
    const next = resolveScroller();
    if (next.isWindow !== previous.isWindow || next.el !== previous.el) bindScrollTarget();
    const { el } = scroller;
    const v = computeVisibility(el.scrollTop, el.scrollHeight, el.clientHeight);
    btnTop.hidden = !v.showTop;
    btnBottom.hidden = !v.showBottom;
    wrap.hidden = !v.showTop && !v.showBottom;
  }

  function schedule() {
    if (!frame) frame = window.requestAnimationFrame(update);
  }

  function jump(toBottom) {
    const { el, isWindow } = scroller;
    const top = toBottom ? el.scrollHeight : 0;
    (isWindow ? window : el).scrollTo({ top, behavior: behavior() });
  }

  const onTop = () => jump(false);
  const onBottom = () => jump(true);
  btnTop.addEventListener("click", onTop);
  btnBottom.addEventListener("click", onBottom);
  bindScrollTarget();
  window.addEventListener("resize", schedule, { passive: true });

  // Content height changes (tab switches, rendered blocks) do not fire scroll.
  const observed = root.querySelector("main") || root.body;
  let resizeObserver = null;
  let mutationObserver = null;
  if (typeof ResizeObserver === "function") {
    resizeObserver = new ResizeObserver(schedule);
    resizeObserver.observe(observed);
  } else if (typeof MutationObserver === "function") {
    mutationObserver = new MutationObserver(schedule);
    mutationObserver.observe(observed, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden"] });
  }

  schedule();

  function destroy() {
    if (frame) window.cancelAnimationFrame(frame);
    frame = 0;
    btnTop.removeEventListener("click", onTop);
    btnBottom.removeEventListener("click", onBottom);
    window.removeEventListener("resize", schedule);
    if (scrollTarget) scrollTarget.removeEventListener("scroll", schedule);
    if (resizeObserver) resizeObserver.disconnect();
    if (mutationObserver) mutationObserver.disconnect();
  }
  return destroy;
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => initScrollJump(), { once: true });
  } else {
    initScrollJump();
  }
}
