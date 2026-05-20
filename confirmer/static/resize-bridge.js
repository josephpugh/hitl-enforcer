// Posts the page's height to the parent window so an embedding chat UI can
// resize the iframe to fit its content. Cross-origin friendly: the parent
// MUST verify event.origin before trusting the value.
(function () {
  function post() {
    if (window.parent === window) return;
    var h = Math.ceil(document.documentElement.scrollHeight);
    window.parent.postMessage({ type: 'hitl_resize', height: h }, '*');
  }
  window.addEventListener('load', post);
  if ('ResizeObserver' in window) {
    try {
      new ResizeObserver(post).observe(document.documentElement);
    } catch (e) {
      // Some browsers throw on observing documentElement; fall back to window resize.
      window.addEventListener('resize', post);
    }
  } else {
    window.addEventListener('resize', post);
  }
})();
