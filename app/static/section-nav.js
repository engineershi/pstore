/* Collapsible section navigator.
   Wires up the tiny floating button (.secfab): tap to expand the menu of
   in-page sections, tap again (or pick one, click away, or press Escape) to
   collapse. While the page scrolls it highlights the section currently in view
   and reveals the back-to-top pill (.totop). */
(function () {
  function init() {
    var fab = document.querySelector('.secfab');
    if (!fab) return;
    var btn = fab.querySelector('.secfab-btn');
    var menu = fab.querySelector('.secfab-menu');
    var links = menu ? [].slice.call(menu.querySelectorAll('a')) : [];
    var secs = links.map(function (a) {
      return document.querySelector(a.getAttribute('href'));
    });
    var tt = document.querySelector('.totop');

    function setOpen(on) {
      fab.classList.toggle('open', on);
      if (btn) btn.setAttribute('aria-expanded', on ? 'true' : 'false');
    }
    function isOpen() { return fab.classList.contains('open'); }

    if (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        setOpen(!isOpen());
      });
    }
    links.forEach(function (a) {
      a.addEventListener('click', function () { setOpen(false); });
    });
    document.addEventListener('click', function (e) {
      if (isOpen() && !fab.contains(e.target)) setOpen(false);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && isOpen()) setOpen(false);
    });

    function paint() {
      var top = window.scrollY + 140, idx = 0;
      for (var i = 0; i < secs.length; i++) {
        if (secs[i] && secs[i].offsetTop <= top) idx = i;
      }
      links.forEach(function (a, i) { a.classList.toggle('active', i === idx); });
      if (tt) tt.classList.toggle('show', window.scrollY > 300);
      var active = links[idx];
      if (active && isOpen() && menu && menu.scrollHeight > menu.clientHeight) {
        menu.scrollTop = Math.max(
          0, active.offsetTop - menu.clientHeight / 2 + active.offsetHeight / 2);
      }
    }
    window.addEventListener('scroll', paint, { passive: true });
    window.addEventListener('resize', paint, { passive: true });
    paint();

    window.scrollToSection = function (sel) {
      var el = document.querySelector(sel);
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      setOpen(false);
    };
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
