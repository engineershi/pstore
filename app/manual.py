# -*- coding: utf-8 -*-
"""pstore user manual: the visual + printable guide to running the app at its
highest form. Two outputs from one source of truth:

  * render_admin_manual() -> the in-app admin page (/admin/manual), fully
    cross-linked to every tool, page and feature on the site.
  * build_pdf()           -> a styled, printable PDF companion that drops the
    links (a document can't click) but keeps every step, tip and checklist.

Pure stdlib: HTML for the admin page, pdfgen for the PDF.
"""

import html as _html

import pdfgen

# The accent + style tag used by the PDF cover so it matches the pstore brand.
_ACCENT = (255, 107, 44)
_BG = (255, 253, 247)


def _esc(s):
    return _html.escape(s, quote=True)


# --------------------------------------------------------------------------
# PDF build — a clean, printable companion to the on-page manual.
# pdfgen runs in Latin-1, so we stick to plain prose + bullets (no box art).
# --------------------------------------------------------------------------
def build_pdf():
    doc = pdfgen.Pdf(accent=_ACCENT, bg=_BG)
    doc.cover(
        "The pstore User Guide",
        "How to run this software at its highest form: mine niches, rank on "
        "Google, capture emails, send the 5-part buyer sequence, publish on "
        "social and keep your data fresh — step by step.",
        kicker="OWNER MANUAL",
    )

    doc.heading("What pstore is")
    doc.paragraph(
        "pstore is a self-contained Amazon affiliate business in a box. You give "
        "it one seed keyword and it builds a complete money-making machine: ranked "
        "review pages, a sales landing page, an email opt-in with a 5-part buyer "
        "sequence, an AI-written PDF lead magnet, social post kits, an SEM keyword "
        "brief, an SEO audit, click analytics and an automatic data refresher. "
        "Every Amazon link carries your affiliate tag.")
    doc.paragraph(
        "The business runs on a loop: a visitor lands on your niche page (from "
        "Google, social or email), reads the ranked picks, clicks your tagged "
        "Amazon link and buys. Along the way they can opt in to email, and the "
        "5-part sequence turns those free visitors into repeat buyers. One niche "
        "is nice; many niches, each with the loop running, is the game.")
    doc.pullquote("One seed keyword in. A full selling funnel out.")
    doc.page_break()

    doc.heading("The 4 phases")
    doc.paragraph("Every niche moves through four phases. Run all four, every time.")
    doc.bullets([
        "1. Attract — get people to the page (SEO niche pages, social posts, email).",
        "2. Convert — turn visitors into subscribers via the opt-in form.",
        "3. Deliver — send the free ebook and the 5-part email sequence.",
        "4. Multiply — use analytics to double down, and add more niches.",
    ])
    doc.paragraph(
        "These four stages are built right into the Marketing Workbench (/tool), "
        "so the app itself is your roadmap.")
    doc.page_break()

    doc.heading("Step 1 — Mine a niche")
    doc.paragraph(
        "Mine on the Marketing Workbench (/tool) via Launch marketing, or call the "
        "mining API. Enter a broad buying seed such as 'air fryer' or 'trail "
        "camera'. pstore runs your seed through Amazon's own keyless autosuggest "
        "and product search, then reports a demand score (0-10), a saturation "
        "score (0-10) and a magnet product — the best entry offer by reviews and "
        "price.")
    doc.bullets([
        "Pick seeds with high demand and lower saturation first.",
        "Saving a niche auto-generates its review page and auto-submits it to IndexNow.",
        "One seed expands into several sub-niches from autosuggest — save them all.",
    ])
    doc.page_break()

    doc.heading("Step 2 — Rank on Google (SEO)")
    doc.paragraph(
        "Open the SEO audit (/admin/seo) and check the site health strip. Every "
        "niche should show 'indexable'; fix any row with a red badge (title too "
        "long, missing description, no products). Then open the Search Funnel "
        "(/admin/sem) for each niche to get an intent brief, long-tail keywords "
        "and people-also-ask prompts that tell you exactly what to add.")
    doc.paragraph(
        "Indexing is automatic: the moment a niche is saved, pstore submits the "
        "page to IndexNow for near-instant Bing and Google discovery. Confirm the "
        "key is live on the Keys page (/keys).")
    doc.paragraph(
        "To also reach Google, Bing and Yandex at full speed, open the Search "
        "Engines hub (/admin/seoengines). Paste each engine's verification token "
        "on the Keys page (or set them via env), then click 'Submit sitemap'. "
        "The hub also shows per-engine referral traffic collected by your own "
        "beacon — no external token needed for that view.")
    doc.page_break()

    doc.heading("Step 3 — Capture the email")
    doc.paragraph(
        "Every niche page and the homepage already render an opt-in form that "
        "collects first name and email. The first name is used to personalise "
        "every email ('Hi Jane,' not 'Hi there'), which lifts open rates.")
    doc.bullets([
        "Emails are only sent to opted-in subscribers.",
        "Re-subscribing reactivates a previously unsubscribed address.",
        "An unsubscribe link and List-Unsubscribe header are added to every email.",
    ])
    doc.page_break()

    doc.heading("The landing page — CMS sales page")
    doc.paragraph(
        "Every niche gets a dedicated, fully editable sales landing page at "
        "/lp/<slug>. It is the money page: it turns cold traffic (from Google, "
        "social, QR codes or emails) into subscribers and Amazon clicks. You run "
        "it entirely from the Landing Pages editor (/admin/cms) — no code.")
    doc.bullets([
        "Pick a style template in one click: sunset (warm default), clean, forest, ocean or midnight (dark). The preset re-skins the entire page instantly.",
        "Feature toggles switch the promo banner + discount code, the countdown timer, the sticky buy button and reveal animations on or off per niche.",
        "Every section has its own show/hide switch: hero, social proof, product spotlight, email gate, testimonials, urgency, guarantee, FAQ and more.",
        "Generate copy rebuilds all section text from the niche in one click; Apply preset re-skins without touching your custom copy — the two never fight.",
    ])
    doc.paragraph(
        "The page is persuasion-engineered (Suby's How to Sell Like Crazy + "
        "Cialdini's Influence): live-data social proof, scarcity counters, an "
        "honest disclosure and a reciprocity offer — a free niche PDF guide "
        "delivered the moment a visitor opts in. Email gate on means the PDF is "
        "unlocked by a short-lived token issued on subscribe; flip the PDF gate "
        "off to hand out the PDF with no email wall. Landing pages are indexed "
        "too: each /lp/<slug> appears in your sitemap with a canonical URL.")
    doc.page_break()

    doc.heading("Step 4 — Send the 5-part buyer sequence")
    doc.paragraph(
        "Every subscriber enters a 5-email sequence built from the niche's top "
        "pick: (1) hook and value, (2) social proof, (3) objections, (4) soft "
        "urgency, (5) follow-up and review request. Each email carries your tagged "
        "Amazon link.")
    doc.paragraph(
        "Email Studio (/admin/emails) is the one place to compose and send mail: "
        "pick any niche, segment (hot/warm/cold/converted/inactive) or typed "
        "address list, choose a sequence step, converted follow-up, re-engage or "
        "a fully custom subject + body, then deliver now or schedule a slot (UTC). "
        "A live preview shows exactly what lands in the inbox.")
    doc.bullets([
        "Pick recipients with the checkboxes, or type any address(es) — one per line; the recipient count updates live.",
        "The Studio's tabs organize the whole mail lifecycle: Compose, Inbox (replies), Subscribers (search, filter, unsubscribe/resubscribe/delete), Drafts (save, reopen and edit any composition) and Sent/scheduled (review or cancel scheduled mail).",
        "Switches control the tracked affiliate link, open-tracking pixel, PDF attachment, dedup and sequence progress.",
        "Dry-run first to preview the send counts without emailing anyone.",
        "Auto-send runs the 5-step sequence to every ready subscriber on your chosen UTC hours; the Email Studio page shows the last run and lets you toggle it.",
        "Replies are captured too: the Studio's Inbox tab polls your IMAP mailbox (IMAP_HOST/USER/PASSWORD, or a forwarder hitting /api/cron/inbox) and maps each reply back to the subscriber via a tagged Reply-To address, so you can read, archive, mark read or reply from the same page.",
    ])
    doc.page_break()

    doc.heading("Step 5-6 — Ebook lead magnet and social posts")
    doc.paragraph(
        "The Ebooks page (/admin/ebooks) turns any niche into a designed PDF in "
        "one click. Free AI providers are built in (OpenCode, Mistral, NVIDIA) so "
        "it works with no budget; add an OpenAI key for higher polish.")
    doc.paragraph(
        "The Social page (/admin/social) generates a ready-to-post kit for X, "
        "Facebook, LinkedIn, Instagram, Pinterest and Threads. Each kit has a "
        "platform caption, hashtags and a tracked link with its own code, so every "
        "post's clicks are counted individually in Analytics.")
    doc.page_break()

    doc.heading("Step 7-8 — Launch and keep data fresh")
    doc.paragraph(
        "Instead of doing steps by hand, use Launch marketing on the Workbench "
        "(/tool): it builds the entire funnel for a niche and prints a status "
        "strip showing the landing page is live, IndexNow queued, ebook ready and "
        "clicks tracking. The Workbench also provides DMs, the review pipeline, "
        "boost campaigns, text links, Markdown and QR codes.")
    doc.paragraph(
        "The Data Refresh page (/admin/refresh) keeps prices and ratings accurate. "
        "A background loop re-mines stale niches on a schedule (interval, staleness "
        "window and per-cycle cap are configurable). Use 'Refresh now' per niche or "
        "'Refresh all now' for a manual pass.")
    doc.pullquote("Fresh data protects trust — and trust protects commissions.")
    doc.page_break()

    doc.heading("Highest-form playbook")
    doc.bullets([
        "Set the affiliate tag and SMTP first — nothing else matters until those work.",
        "Stack traffic: SEO (compounding) + social (fast) + email (cheap) + QR and landing pages (offline).",
        "Watch Analytics and double down on the most-clicked products and best sources.",
        "Choose niches like a fund: high demand, lower saturation first.",
        "Comply always: opted-in emails, FTC/Associates disclosure, direct tagged links.",
    ])
    doc.paragraph(
        "Checklist for a new niche: pick a seed, mine it, confirm indexable in the "
        "SEO audit, note the SEM long-tails, Launch marketing, generate the ebook, "
        "publish one or two social posts, dry-run then send the emails, confirm "
        "auto-refresh is on, and paste the text links and QR code anywhere relevant.")
    return doc.save()


def _pdf_bytes():
    return build_pdf()


# --------------------------------------------------------------------------
# HTML admin page — the in-app manual, fully cross-linked.
# --------------------------------------------------------------------------
def render_admin_manual(nav_html, totop_html):
    def a(href, label, note=None):
        note_txt = ('<span class="n">%s</span>' % _esc(note)) if note else ""
        return '<a class="btn" style="width:100%%" href="%s">%s %s</a>' % (
            _esc(href), _esc(label), note_txt)

    # A reference grid linking to every tool, page and feature.
    tools = "".join([
        a("/dashboard", "🧭 Dashboard", "everything"),
        a("/tool", "🛠 Workbench", "launch + funnel"),
        a("/keys", "🔑 Keys", "affiliate tag + endpoints"),
        a("/admin/emails", "📨 Email Studio", "compose + schedule"),
        a("/admin/cms", "🎨 Landing pages", "presets + toggles"),
        a("/admin/ebooks", "📕 Ebooks", "PDF lead magnet"),
        a("/admin/analytics", "📊 Analytics", "clicks + sources"),
        a("/admin/social", "📣 Social", "tracked posts"),
        a("/admin/sem", "🎯 SEM", "keywords + funnel"),
        a("/admin/seo", "🔍 SEO audit", "indexability"),
        a("/admin/refresh", "📡 Refresh", "auto + manual"),
        a("/admin", "🗺 All pages", "hub"),
    ])
    pages = "".join([
        a("/", "🏠 Home", "public landing"),
        a("/n/air-fryer", "/n/&lt;slug&gt;", "niche review page"),
        a("/lp/air-fryer", "/lp/&lt;slug&gt;", "sales landing page"),
        a("/sitemap.xml", "🗺 Sitemap", "xml"),
        a("/robots.txt", "🤖 Robots", "txt"),
    ])

    body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>User manual — pstore</title>
<link rel="stylesheet" href="/style.css">
<style>.manual-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:10px;margin:6px 0 2px}}
.manual-grid a{{margin:0}}
.manual-grid a .n{{display:block;font-weight:400;font-size:12px;color:var(--muted);margin-top:2px}}
.toc-nav{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:10px 0 2px}}
.toc-nav a{{display:block;padding:10px 12px;border:1px solid var(--border);border-radius:12px;text-decoration:none;color:var(--text);font-size:13.5px;font-weight:700;background:#fff}}
.toc-nav a:hover{{border-color:var(--accent);box-shadow:var(--shadow)}}
.manual h3{{margin:26px 0 6px;font-size:17px}}
.manual h3 a{{text-decoration:none;color:var(--text)}}
.manual h3 a:hover{{color:var(--accent)}}
.manual h3 .step{{color:var(--accent);font-weight:800}}
.step-list{{list-style:none;padding:0;margin:8px 0}}
.step-list li{{padding:6px 0 6px 26px;position:relative}}
.step-list li::before{{content:"✓";position:absolute;left:2px;top:6px;color:var(--accent);font-weight:800}}
.diagram{{background:#fff;border:1px solid var(--border);border-radius:14px;padding:16px 18px;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;line-height:1.55;color:#4a4456;overflow-x:auto;white-space:pre;margin:10px 0}}
.diagram b{{color:var(--accent)}}
.tooltag{{display:inline-block;background:#fff;border:1px solid var(--border);border-radius:999px;padding:2px 10px;font-size:12px;margin:2px 4px 2px 0;color:var(--accent);text-decoration:none;font-weight:700}}
.tooltag:hover{{border-color:var(--accent)}}
.dlbar{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:4px 0 2px}}
/* reader toolbar (this page only): a slim, sticky, horizontal control docked
   under the page header so the reading width is one glance away while scrolling */
#manual-main{{max-width:100%;margin:0;padding:22px 20px 60px;transition:width .18s ease}}
.readctl{{position:sticky;top:0;z-index:60;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  background:rgba(255,252,247,.92);backdrop-filter:blur(8px);border-bottom:1px solid var(--border);
  padding:10px 22px;font-size:13px}}
.readctl .ctl-label{{font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}}
.seg{{display:inline-flex;background:#fff;border:1px solid var(--border);border-radius:999px;padding:3px;gap:2px}}
.seg button{{border:none;background:transparent;border-radius:999px;padding:5px 13px;font-size:12.5px;font-weight:700;
  cursor:pointer;color:var(--muted)}}
.seg button:hover{{color:var(--text)}}
.seg button.on{{background:var(--accent);color:#fff}}
.readctl .sep{{width:1px;height:22px;background:var(--border)}}
.readctl input[type=range]{{width:170px;accent-color:var(--accent);cursor:pointer}}
.readctl .size{{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--muted);min-width:40px;text-align:right}}
.readctl .px{{color:var(--muted);font-size:12px}}
.readctl .hint{{color:var(--muted);font-size:12px}}
#reader{{overflow-x:auto;scroll-margin-top:80px}}
</style>
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>User <span>manual.</span></h1>
<p class="tagline">The complete, visual guide to running pstore at its highest form — chip every tool on this page is a real, clickable shortcut.</p></div>
{nav_html}
</header>
<div class="readctl" id="readctl" role="toolbar" aria-label="Reading width controls">
  <span class="ctl-label">Reading width</span>
  <span class="seg">
    <button data-w="760" title="Narrow column">Narrow</button>
    <button data-w="1080" class="on" title="Default column">Normal</button>
    <button data-w="1400" title="Wide column">Wide</button>
    <button data-w="100" title="Full page width (100%)">Full</button>
  </span>
  <span class="sep"></span>
  <input type="range" id="readr" min="640" max="2400" step="10" value="1080" title="Drag to set the reading window width">
  <span class="size" id="readsize">1080</span><span class="px">px</span>
</div>
<main id="manual-main">
<div class="reader" id="reader">
<section class="card"><h2>📕 The manual — download it</h2>
<div class="dlbar">
  <a class="btn warm" href="/admin/manual.pdf" download>Download PDF manual ⬇</a>
  <a class="btn" href="#toc">Jump to contents ↧</a>
  <a class="btn ghost" href="/admin">🗺 Back to all pages</a>
</div>
<p class="hint" style="margin-top:10px">A styled, printable PDF companion (no links — a document can't click). The section below is fully linked. Use the controls above to widen the reading window up to full page.</p>
</section>

<section class="card"><h2>🧭 Every tool &amp; feature — one click</h2>
<div class="manual-grid">{tools}</div></section>

<section class="card"><h2>🌐 Public site — the pages you rank &amp; sell from</h2>
<div class="manual-grid">{pages}</div></section>

<section class="card manual" id="toc"><h2>🧾 Contents</h2>
<nav class="toc-nav">
  <a href="#what">1 · What pstore is</a>
  <a href="#phases">2 · The 4 phases</a>
  <a href="#s1">3 · Step 1 · Mine</a>
  <a href="#s2">4 · Step 2 · SEO</a>
  <a href="#s3">5 · Step 3 · Capture</a>
  <a href="#lp">6 · Landings · CMS</a>
  <a href="#s4">7 · Step 4 · Email</a>
  <a href="#s5">8 · Step 5-6 · Ebook + Social</a>
  <a href="#s6">9 · Step 7-8 · Launch + Refresh</a>
  <a href="#playbook">10 · Highest-form playbook</a>
  <a href="#checklist">11 · 30-min checklist</a>
  <a href="#team">12 · Team access &amp; roles</a>
  <a href="#inbox">13 · Inbox &amp; conversations</a>
</nav>

<h3 id="what">1 · What pstore is</h3>
<p>pstore is a <b>self-contained Amazon affiliate business in a box</b>. Give it one seed keyword and it builds a complete machine: ranked review pages, a sales landing page, an email opt-in with a 5-part buyer sequence, an AI-written PDF lead magnet, social post kits, an SEM brief, an SEO audit, click analytics and an automatic data refresher — every Amazon link carrying your affiliate tag.</p>
<div class="diagram">YOU give one seed        pstore auto-builds
        air fryer           ├─ review page  /n/air-fryer   (SEO)
            │               ├─ landing page /lp/air-fryer  (convert)
            ▼               ├─ ranked products (demand + saturation)
     amazon autosuggest     ├─ email opt-in + 5-part sequence
     + product search       ├─ free PDF ebook (AI)
            │               ├─ social post kits + QR codes
            ▼               ├─ SEM keywords + SEO audit
      DEMAND 0-10           └─ click analytics + auto refresh
      SATUR. 0-10                ──► every link tagged ?tag=YOURS</div>
<p>The <b>money loop</b>: visitor lands (Google / social / email) → reads ranked picks → clicks your tagged Amazon link → buys → you earn.</p>

<h3 id="phases">2 · The 4 phases (mental model)</h3>
<p>Run all four for every niche. This exact funnel is built into the <a class="tooltag" href="/tool">🛠 Workbench</a>.</p>
<div class="diagram"> 1 ATTRACT      2 CONVERT      3 DELIVER      4 MULTIPLY
  get traffic  capture email  send ebook    doubles down
  (seo, social)→(opt-in form) →+ 5 emails   →(analytics, niches)</div>

<h3 id="s1">3 · Step 1 — Mine a niche</h3>
<p>On the <a class="tooltag" href="/tool">🛠 Workbench</a> → <b>Launch marketing</b>, or call the mining API. Enter a broad buying seed (<i>air fryer</i>, <i>trail camera</i>). pstore reports a <b>demand score</b>, a <b>saturation score</b> and a <b>magnet product</b> — the best entry offer.</p>
<ul class="step-list">
<li>Pick seeds with <b>high demand</b> and <b>lower saturation</b> first.</li>
<li>Saving a niche auto-builds its review page and auto-submits it to <b>IndexNow</b>.</li>
<li>One seed expands into up to <b>5 sub-niches</b> from autosuggest — save them all.</li>
</ul>

<h3 id="s2">4 · Step 2 — Rank on Google (SEO)</h3>
<p>Open the <a class="tooltag" href="/admin/seo">🔍 SEO audit</a> and check the <b>site health strip</b> — every niche should be <i>indexable</i>. Fix red badges (title too long, missing description, no products). Then open <a class="tooltag" href="/admin/sem">🎯 SEM</a> for each niche: intent brief, long-tail keywords and people-also-ask prompts tell you exactly what to add to rank.</p>
<p>Indexing is <b>automatic</b> — saving a niche pings IndexNow. Verify the key on <a class="tooltag" href="/keys">🔑 Keys</a>. For the three big consoles, open <a class="tooltag" href="/admin/seoengines">🔎 Engines</a>: connect Google Search Console or Yandex via OAuth, paste your Bing API key, then <b>Fetch stats</b> pulls real clicks/impressions per engine and <b>Submit sitemap</b> re-pings it. Until a console is connected, the Traffic panel shows referral-attributed page views + clicks from your own on-site beacon.</p>

<h3 id="s3">5 · Step 3 — Capture the email</h3>
<p>Every niche page and the homepage already render an <b>opt-in form</b> that collects first name + email. The first name personalises every email (<i>"Hi Jane,"</i>) to lift open rates.</p>
<ul class="step-list">
<li>Only <b>opted-in</b> subscribers are emailed.</li>
<li>Re-subscribing reactivates an unsubscribed address.</li>
<li>Every email carries an <b>unsubscribe</b> link + <code>List-Unsubscribe</code>.</li>
</ul>

<h3 id="lp">6 · The landing page — CMS sales page</h3>
<p>Every niche gets a dedicated, fully editable <b>sales landing page at <code>/lp/&lt;slug&gt;</code></b> — the money page that turns cold traffic (Google, social, QR codes, email) into subscribers and Amazon clicks. You run it entirely from the <a class="tooltag" href="/admin/cms">🎨 Landing pages</a> editor, no code.</p>
<ul class="step-list">
<li><b>Style templates in one click:</b> sunset (warm default), clean, forest, ocean or midnight (dark) — the preset re-skins the entire page, including dark mode.</li>
<li><b>Feature toggles per niche:</b> promo banner + discount code, countdown timer, sticky buy button and reveal animations — each on/off.</li>
<li><b>Every section has a show/hide switch:</b> hero, social proof, product spotlight, email gate, testimonials, urgency, guarantee, FAQ and more.</li>
<li><b>Generate copy</b> rebuilds all section text from the niche in one click; <b>Apply preset</b> re-skins without touching your custom copy — the two never fight.</li>
<li><b>Email-gated PDF:</b> the free guide unlocks with a short-lived token the moment a visitor subscribes; flip the <b>PDF gate</b> off to hand out the PDF with no email wall.</li>
<li>Landing pages are <b>indexed too</b>: each <code>/lp/&lt;slug&gt;</code> appears in your <a class="tooltag" href="/sitemap.xml">🗺 Sitemap</a> with a self-referencing canonical.</li>
</ul>
<p>It is persuasion-engineered (Suby + Cialdini): live-data social proof, scarcity counters, an honest disclosure and a reciprocity offer — every Amazon link tagged with your affiliate code.</p>

<h3 id="s4">7 · Step 4 — Send the 5-part buyer sequence</h3>
<p>Every subscriber enters a 5-email sequence from the top pick: hook → social proof → objections → soft urgency → follow-up + review. Each carries your tagged Amazon link.</p>
<p><a class="tooltag" href="/admin/emails">📨 Email Studio</a> is the one place to compose and send mail: pick any niche, segment (hot/warm/cold/converted/inactive) or a typed address list, choose a sequence step, converted follow-up, re-engage or a fully custom subject + body, then deliver <b>now</b> or <b>schedule a UTC slot</b>. A live preview shows exactly what lands in the inbox.</p>
<ul class="step-list">
<li>Pick recipients with the checkboxes, or type any address(es) — one per line; the count updates live.</li>
<li>Switches control the <b>tracked link</b>, <b>open pixel</b>, <b>PDF attachment</b>, <b>dedup</b> and <b>sequence progress</b>.</li>
<li><b>Dry-run</b> first to preview the counts without emailing anyone.</li>
<li><b>Auto-send</b> runs the 5-step sequence to every ready subscriber on your chosen UTC hours; the Studio shows the last run and lets you toggle it on/off.</li>
</ul>

<h3 id="s5">8 · Step 5-6 — Ebook lead magnet + social posts + boosts</h3>
<p>The <a class="tooltag" href="/admin/ebooks">📕 Ebooks</a> page turns any niche into a designed PDF in one click. Free AI providers are built in (OpenCode, Mistral, NVIDIA); add an OpenAI key for extra polish. The <a class="tooltag" href="/admin/social">📣 Social</a> page generates a ready-to-post kit for X, Facebook, LinkedIn, Instagram, Pinterest and Threads — each with a <b>tracked link</b>, so every post's clicks are counted individually in <a class="tooltag" href="/admin/analytics">📊 Analytics</a>.</p>
<p>The <a class="tooltag" href="/tool">🛠 Workbench</a> <b>Marketing boosts</b> panel mints a real, UTM-tracked campaign per promo angle (<b>▶ Run</b> persists it, folds in SEM long-tail phrases, warms the lead-magnet PDF and pings IndexNow; <b>⟶ Social page</b> publishes one as a live, attributed Boost post). Each boost keeps a stable link so its clicks aggregate over time.</p>

<h3 id="s6">9 · Step 7-8 — Launch + keep data fresh</h3>
<p>Use <b>Launch marketing</b> on the <a class="tooltag" href="/tool">🛠 Workbench</a> to build the whole funnel in one click, then see a status strip (landing live · IndexNow queued · ebook ready · clicks tracking). The Workbench also has <b>DM scripts, review pipeline, boost campaigns, text links, Markdown and QR codes</b>.</p>
<p>The <a class="tooltag" href="/admin/refresh">📡 Refresh</a> page keeps prices and ratings accurate — a background loop re-mines stale niches automatically, and you can <b>Refresh now</b> per niche or <b>Refresh all now</b>.</p>

<h3 id="playbook">10 · Highest-form playbook</h3>
<ul class="step-list">
<li>Set the <b>affiliate tag</b> and <b>SMTP</b> first — nothing else matters until those work.</li>
<li><b>Stack traffic</b>: SEO (compounding) + social (fast) + email (cheap) + QR/landing pages (offline).</li>
<li>Watch <a class="tooltag" href="/admin/analytics">📊 Analytics</a> and double down on the most-clicked products and best sources. It now also shows <b>page views</b> (leads + public site) and <b>lead-page interactions</b> — promo taps, countdown, sticky-CTAs and PDF downloads — so you can optimize the landing page itself, not just the links.</li>
<li>Choose niches like a fund: <b>high demand, lower saturation</b> first.</li>
<li><b>Comply always</b>: opted-in email, FTC/Associates disclosure, direct tagged links.</li>
</ul>

<h3 id="checklist">11 · 30-minute zero-to-sale checklist</h3>
<ul class="step-list">
<li>Pick a seed (high demand, lower saturation)</li>
<li>Mine it → review demand/saturation → save the niche</li>
<li><a class="tooltag" href="/admin/seo">🔍 SEO</a> → confirm indexable, fix reds</li>
<li><a class="tooltag" href="/admin/sem">🎯 SEM</a> → note long-tails + PAA prompts</li>
<li><a class="tooltag" href="/admin/cms">🎨 Landings</a> → pick a preset, set toggles + section switches, generate copy</li>
<li><a class="tooltag" href="/tool">🛠 Workbench</a> → Launch marketing</li>
<li><a class="tooltag" href="/admin/ebooks">📕 Ebooks</a> → generate the PDF</li>
<li><a class="tooltag" href="/admin/social">📣 Social</a> → publish 1-2 platforms</li>
<li><a class="tooltag" href="/admin/emails">📨 Email Studio</a> → dry-run → send first batch</li>
<li><a class="tooltag" href="/admin/refresh">📡 Refresh</a> → confirm auto-refresh is on</li>
<li>Paste text links / Markdown / QR anywhere relevant</li>
</ul>

<h3 id="team">12 · Team access &amp; roles</h3>
<p>pstore can run as a small team. From the <a class="tooltag" href="/admin/login">🔐 login page</a>, a colleague picks <b>Request an account</b>, signs up with their work email and gets a <b>branded confirmation email</b> — the account stays <i>unverified</i> until they click that link. Clicking the confirmation link <b>activates the account and signs them straight in</b>: colleagues with tool access land on the 🧭 Dashboard, everyone else lands on a welcome/onboarding page until the owner grants roles on <a class="tooltag" href="/admin/users">👥 Users &amp; roles</a>.</p>
<ul class="step-list">
<li>Every tool belongs to <b>one function</b>: Idea tools, Email Studio, Social publisher, SEO &amp; consoles, Content (CMS/ebooks), Marketing &amp; ROI, Analytics &amp; backup, Keys &amp; API keys.</li>
<li>A <b>role</b> is a custom matrix of functions — create your own (<i>Writer</i> = content only, <i>Operator</i> = daily tools) and tick whatever fits. A user can hold <b>several roles</b>; their access is the union of the functions on all of them.</li>
<li>Pages <b>and</b> their <code>/api/…</code> endpoints are both gated by function — no role for a tool means the <b>403</b> screen and its API answers <i>forbidden</i>.</li>
<li>The owner is always allowed everywhere; only the owner sees <b>Users &amp; roles</b> and can create/disable users, change roles and reset passwords.</li>
<li>Signing in only works after the <b>email is verified</b>; disabling an account kills its sessions instantly.</li>
<li>Every team member has a <b>🧭 personal dashboard</b> (their landing page after the confirmation link) showing their profile, status, every <b>assigned role</b>, and one-click cards for each granted tool — plus a sign-out link.</li>
<li>Team members can <b>edit their own profile</b> right on the dashboard (<i>Edit profile</i>): update their display name, or set a new password with the same minimum-policy rules as signup. Their session stays signed in after a name change; we never hand out or reset other members' passwords.</li>
<li><b>Forgotten password?</b> The login page has a <i>Forgot your password?</i> link → a secure, <b>single-use reset link</b> is emailed (valid 1 hour). It never leaks whether an email has an account; the owner login is environment-based and never reset this way.</li>
<li><b>No SMTP configured?</b> Registration and <i>resend</i> will then say so honestly on the page instead of promising an inbox message that can't arrive (the confirmation link needs real e-mail out). The owner either sets up <code>SMTP_HOST/USER/PASSWORD</code>, or skips e-mail entirely by creating the account as <b>verified</b> directly on the Users page.</li>
<li>Confirmation links are HMAC-signed and expire after <b>72 hours</b>; anyone who loses one can re-request it from the login page (<i>resend my confirmation link</i>).</li>
</ul>

<h3 id="inbox">13 · The Inbox — read, reply to and file your mail</h3>
<p>The <a class="tooltag" href="/admin/emails">📨 Email Studio</a> also runs your <b>conversations</b>, not just your broadcasts. Every send to a subscriber carries a <b>unique tagged Reply-To</b> (<code>pstore+&lt;subscriber-id&gt;@yourdomain</code>) — set <code>PSTORE_REPLY_DOMAIN</code> to the domain whose mailbox you watch. When a customer replies, the Studio's <b>Inbox</b> tab captures it: either it polls your <b>IMAP</b> mailbox (<code>IMAP_HOST / IMAP_USER / IMAP_PASSWORD</code>) every 60 seconds, or any forwarder can <code>POST /api/cron/inbox</code> with your <code>EMAIL_CRON_SECRET</code> to trigger a pull on demand.</p>
<ul class="step-list">
<li>Every reply is <b>linked to its subscriber</b>: the tag in the Reply-To wins, with a from-address match as fallback, so you always know who wrote.</li>
<li>Unread messages carry a count on the <b>Inbox</b> tab; open a message to read it, then <b>Reply</b> right from the studio — the answer threads (<code>Re:</code>, <code>In-Reply-To</code>) in the customer's mail client and comes back to this same page.</li>
<li>Mark read/unread, <b>archive</b> or <b>delete</b> any message — pure CRUD, nothing hidden.</li>
<li>Replies you send appear in the studio's <b>Recent activity</b> alongside your campaigns.</li>
<li><code>SMTP_REPLY_TO</code> (single address) still works as the fallback; the per-subscriber tag simply makes every reply attributable.</li>
<li>The Studio covers the whole mail lifecycle in tabs: <b>Compose</b>, <b>Inbox</b> (replies), <b>Subscribers</b> (search, status filter, unsubscribe/resubscribe/delete), <b>Drafts</b> (save a composition, reopen and edit it later) and <b>Sent</b> (outbound history with cancel for still-scheduled mail).</li>
</ul>
</section>
</div>
</main>
<footer><p>User manual — owner section, never indexed. <a href="/admin">All pages</a> · <a href="/admin/logout">Log out</a>.</p></footer>
<script>
(function(){{
  var main=document.getElementById('manual-main'),
      reader=document.getElementById('reader'),
      ctl=document.getElementById('readctl'),
      slider=document.getElementById('readr'),
      size=document.getElementById('readsize'),
      buttons=Array.from(ctl.getElementsByTagName('button'));
  var maxW=Math.max(window.innerWidth-40, 640);
  var saved=null;
  try{{ saved=localStorage.getItem('pstore.manual.w'); }}catch(e){{}}
  function apply(wst){{
    var full = (wst==='100%');
    if(full){{ slider.value=slider.max; size.textContent='Full'; main.style.width='100%'; }}
    else{{ var w=Math.min(Math.max(parseInt(wst,10)||1080,640),maxW);
      slider.value=w; size.textContent=w+''; main.style.width=w+'px'; }}
    buttons.forEach(function(b){{ var on=(b.getAttribute('data-w')==='100'&&full)||((b.getAttribute('data-w')!=='100')&&!full&&parseInt(b.getAttribute('data-w'),10)===parseInt(main.style.width,10)); b.classList.toggle('on',on); }});
    try{{ localStorage.setItem('pstore.manual.w', full?'100%':String(main.style.width.replace('px',''))); }}catch(e){{}}
  }}
  buttons.forEach(function(b){{
    b.addEventListener('click', function(){{ apply(b.getAttribute('data-w')==='100' ? '100%' : b.getAttribute('data-w')); }});
  }});
  slider.addEventListener('input', function(){{ apply(String(slider.value)); }});
  apply(saved || 1080);
}})();
</script>
{totop_html}
</body></html>"""
    return body
