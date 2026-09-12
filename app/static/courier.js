/* pstore courier: opt-in capture + affiliate-click analytics (stdlib-only sniff). */
(function () {
  "use strict";
  var main = document.querySelector("main[data-niche]");
  var slug = main ? (main.getAttribute("data-niche") || "page") : "page";
  var baseSource = main ? (main.getAttribute("data-source") || "page") : "page";

  /* Social-post attribution: when the page was reached via a UTM-tagged link
     (utm_source=<platform>, utm_content=<post-code>), every click on it is
     credited to that exact post so /admin/social + /admin/analytics can score
     per-post traffic and conversions. */
  var params = new URLSearchParams(location.search || "");
  var utmSource = params.get("utm_source") || "";
  var utmContent = params.get("utm_content") || "";
  /* A/B headline variant (set by the server on <main data-variant>: stick the
     variant id into click attribution even when there is no UTM content, so the
     analytics report can score which headline converts. */
  var abVariant = main ? main.getAttribute("data-variant") || "" : "";
  if (!utmContent && abVariant) utmContent = "ab-" + abVariant;

  /* ---- email opt-in: <form class="courier">, POST /subscribe, JSON ---- */
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form.classList || !form.classList.contains("courier")) return;
    ev.preventDefault();
    var email = form.querySelector("[name=email]");
    var first_name = form.querySelector("[name=first_name]");
    var keyword = form.querySelector("[name=keyword]");
    var note = form.querySelector(".courier-msg");
    var val = (email && email.value || "").trim();
    if (!val) {
      if (note) note.textContent = "Please add an email address.";
      return;
    }
    var btn = form.querySelector("button[type=submit], .courier-cta");
    function lock(locked) {
      if (!btn) return;
      btn.disabled = locked;
      if (locked) btn.setAttribute("aria-busy", "true");
      else btn.removeAttribute("aria-busy");
    }
    function fail(msg) {
      lock(false);
      if (note) {
        note.style.display = "block";
        note.textContent = msg || "That didn't work — please try again.";
      }
    }
    lock(true);
    fetch("/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: val,
        first_name: (first_name && first_name.value || "").trim(),
        keyword: (keyword && keyword.value) || slug,
        source: (main && main.dataset.source) || "niche"
      })
    }).then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          document.body.setAttribute("data-opted", "1");
          lock(true); /* stay locked — the lead is in */
          if (note) {
            note.style.display = "block";
            note.textContent = d.message || "You're in — check your inbox.";
          }
        } else {
          fail((d && d.error) || "That didn't work — please try again.");
          return;
        }
        /* Email gate: on success, unlock the gated PDF download. */
        if (d && d.ok && form.classList.contains("gate-form")) {
          var kw = (keyword && keyword.value) || slug;
          var unlock = document.getElementById("gate-unlock");
          if (unlock) {
            unlock.href = "/_gated/pdf?keyword=" + encodeURIComponent(kw) +
                          "&token=" + encodeURIComponent((d.download_token || ""));
            unlock.style.display = "block";
          }
        }
        /* ---- refer-a-friend: show the share link + reward on every capture ---- */
        if (d && d.ok && d.referral_url) {
          showReferral(d.referral_url);
        }
      })
      .catch(function () { fail("Network error — please try again."); });
  });

  /* ---- price-watch modal: "Track this price" cards ---- */
  var watchModal = null;
  function escAttr(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/"/g, "&quot;")
      .replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function buildWatchModal() {
    var s = document.createElement("style");
    s.textContent = ".wm{position:fixed;inset:0;z-index:90;display:none;align-items:center;justify-content:center;background:rgba(15,17,22,.55);padding:16px}." +
      "wm.open{display:flex}.wm .wm-card{background:#fff;color:#23262e;border-radius:14px;max-width:400px;width:100%;padding:22px;box-shadow:0 18px 50px rgba(0,0,0,.25)}." +
      "wm .wm-card h3{margin:0 0 6px;font-size:18px}.wm .wm-card p{color:#5a6270;font-size:13.5px;margin:0 0 12px;line-height:1.5}." +
      "wm .wm-row{display:flex;gap:8px}.wm .wm-row input{flex:1;min-width:0;padding:10px 12px;border:1px solid #d0d4dc;border-radius:8px;font-size:14px}.wm .wm-row button{white-space:nowrap}.wm .wm-close{cursor:pointer;font-weight:700;color:#8a93a2;border:none;background:none;font-size:14px;float:right}";
    document.head.appendChild(s);
    watchModal = document.createElement("div");
    watchModal.className = "wm";
    watchModal.innerHTML = '<div class="wm-card"><button type="button" class="wm-close" aria-label="Close">\u00d7</button>' +
      "<h3>\U0001f514 Price-drop alert</h3>" +
      "<p>Drop your email and we'll ping you the exact moment this product goes on sale — with a direct link to grab it.</p>" +
      '<form class="courier wm-form"><input type="hidden" name="asin"><input type="hidden" name="keyword">' +
      '<input type="text" name="first_name" placeholder="First name (optional)" autocomplete="given-name" style="width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #d0d4dc;border-radius:8px;font-size:14px;margin-bottom:8px">' +
      '<div class="wm-row"><input type="email" name="email" placeholder="you@example.com" required autocomplete="email">' +
      '<button type="submit" class="warm">Watch it</button></div>' +
      '<p class="courier-msg" style="display:none;margin:10px 0 0;font-size:13px"></p></form></div>';
    watchModal.addEventListener("click", function (e) {
      if (e.target === watchModal || e.target.classList.contains("wm-close")) closeWatch();
    });
    document.body.appendChild(watchModal);
  }
  function openWatch(asin, keyword) {
    if (!watchModal) buildWatchModal();
    watchModal.querySelector("[name=asin]").value = asin;
    watchModal.querySelector("[name=keyword]").value = keyword;
    watchModal.classList.add("open");
    var e = watchModal.querySelector("[name=email]");
    setTimeout(function () { e.focus(); }, 40);
  }
  function closeWatch() {
    if (watchModal) {
      watchModal.classList.remove("open");
      var m = watchModal.querySelector(".courier-msg");
      if (m) { m.style.display = "none"; m.textContent = ""; }
    }
  }
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest ? ev.target.closest(".watch-price") : null;
    if (!b) return;
    ev.preventDefault();
    openWatch(b.getAttribute("data-watch-asin") || "", b.getAttribute("data-watch-keyword") || "");
  });
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!watchModal || !form.classList || !form.classList.contains("wm-form")) return;
    ev.preventDefault();
    var email = form.querySelector("[name=email]");
    var first = form.querySelector("[name=first_name]");
    var asin = form.querySelector("[name=asin]").value;
    var kw = form.querySelector("[name=keyword]").value;
    var note = form.querySelector(".courier-msg");
    var btn = form.querySelector("button[type=submit]");
    if (!email.value || !/\S+@\S+\.\S+/.test(email.value)) {
      if (note) { note.style.display = "block"; note.textContent = "Please add a valid email."; }
      return;
    }
    if (btn) { btn.disabled = true; }
    fetch("/price-alert", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: email.value, first_name: first.value,
        asin: asin, keyword: kw, ref: params.get("ref") || "" })
    }).then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          document.body.setAttribute("data-opted", "1");
          if (note) { note.style.display = "block"; note.textContent = d.message || "You're all set — we'll email you on the price drop."; }
          if (btn) btn.remove();
          form.querySelectorAll("input[type=email],input[type=text]").forEach(function (i) { i.style.opacity = "0.5"; });
          if (d.referral_url) showReferral(d.referral_url);
          setTimeout(closeWatch, 4200);
        } else {
          if (note) { note.style.display = "block"; note.textContent = (d && d.error) || "That didn't work — try again."; }
          if (btn) btn.disabled = false;
        }
      })
      .catch(function () {
        if (note) { note.style.display = "block"; note.textContent = "Network error — please try again."; }
        if (btn) btn.disabled = false;
      });
  });

  /* ---- refer-a-friend: "share, we both win" block injected near the lead ---- */
  var refInserted = false;
  function showReferral(url) {
    if (refInserted || !url) { return; }
    refInserted = true;
    var card = document.createElement("div");
    card.style.cssText = "margin-top:14px;padding:12px 14px;border:1px dashed #d8c9a0;border-radius:10px;background:#fffaf0;color:#43380f;font-size:13px;line-height:1.55";
    card.innerHTML = "<strong>\U0001f517 Plus, share the guide — you both win.</strong> " +
      "A friend who grabs it gets the same free guide; you get early access to the next niche guide. " +
      'Your link: <b style="word-break:break-all">' + escAttr(url) + "</b><br>" +
      '<button type="button" style="margin-top:8px;cursor:pointer;border:1px solid #c9b67a;background:#fff;color:#43380f;border-radius:6px;padding:5px 10px;font-size:12px">Copy link</button>';
    var copyBtn = card.querySelector("button");
    copyBtn.addEventListener("click", function () {
      var ta = document.createElement("textarea");
      ta.value = url; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); copyBtn.textContent = "Copied \u2713"; }
      catch (e) { copyBtn.textContent = "Select & copy above"; window.getSelection().selectAllChildren(card.querySelector("b")); }
      document.body.removeChild(ta);
    });
    var target = document.querySelector("main[data-niche] form.courier, main .gate, #gate, form.courier");
    if (!target) return;
    var lead = target.closest ? target.closest("section, .card, div") : target.parentNode;
    (lead || target).appendChild(card);
  }

  /* ---- click beacon: any on-page Amazon link reports (slug, source) ---- */
  document.addEventListener("click", function (ev) {
    var el = ev.target;
    var a = el && el.closest ? el.closest("a") : null;
    if (!a) return;
    var href = a.getAttribute("href") || "";
    if (href.indexOf("amazon.") === -1) return;
    var source = utmSource || a.getAttribute("data-beacon") || baseSource;
    var asin = a.getAttribute("data-asin") || "";
    var payload = {
      slug: slug,
      source: source,
      referrer: (document.referrer || "").slice(0, 200),
      asin: asin,
      content: utmContent
    };
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/api/track",
        new Blob([JSON.stringify(payload)], { type: "application/json" }));
    } else {
      var img = new Image();
      img.src = "/api/track?slug=" + encodeURIComponent(payload.slug) +
                "&source=" + encodeURIComponent(payload.source) +
                "&referrer=" + encodeURIComponent(payload.referrer) +
                "&asin=" + encodeURIComponent(payload.asin) +
                "&content=" + encodeURIComponent(payload.content);
    }
  });
/* ---- pageview beacon: report every public page visit once ---- */
  function beacon(name, extra) {
    var payload = { name: name || "view", slug: slug, page: location.pathname,
                    referrer: (document.referrer || "").slice(0, 200) };
    if (utmSource) payload.source = utmSource;
    if (main && main.dataset.keyword) payload.keyword = main.dataset.keyword;
    if (extra) { for (var k in extra) payload[k] = extra[k]; }
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/api/pageview",
        new Blob([JSON.stringify(payload)], { type: "application/json" }));
    } else {
      var img = new Image();
      img.src = "/api/pageview?name=" + encodeURIComponent(payload.name) +
                "&slug=" + encodeURIComponent(payload.slug) +
                "&page=" + encodeURIComponent(payload.page) +
                "&source=" + encodeURIComponent(payload.source || "") +
                "&referrer=" + encodeURIComponent(payload.referrer || "") +
                "&keyword=" + encodeURIComponent(payload.keyword || "");
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { beacon("view"); });
  } else {
    beacon("view");
  }

  /* ---- micro-interactions: any element tagged [data-ev] reports on click ---- */
  document.addEventListener("click", function (ev) {
    var el = ev.target;
    var mark = el && el.closest ? el.closest("[data-ev]") : null;
    if (!mark) return;
    beacon(mark.getAttribute("data-ev") || "click");
  });

  /* ---- lead capture: gated / review PDF downloads ---- */
  document.addEventListener("click", function (ev) {
    var el = ev.target;
    var a = el && el.closest ? el.closest("a") : null;
    if (!a) return;
    var href = a.getAttribute("href") || "";
    if (href.indexOf("/_gated/pdf") !== -1 || href.indexOf("/admin/ebooks/pdf") !== -1) {
      beacon("lead_pdf");
    }
  });
})();