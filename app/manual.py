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
# PDF build — a 6x9" book-format companion to the on-page manual, drawn by
# pdfgen: styled cover, clickable contents, diagrams, a tickable checklist
# and an About-the-founder chapter. pdfgen runs in Latin-1 (ASCII-safe text).
# --------------------------------------------------------------------------
def _chapters():
    """(num, contents-row label, blurb, drawer fn) in reading order."""
    ACC, SAGE, TEAL, PLUM = (255, 107, 44), (120, 150, 135), (90, 140, 150), (140, 120, 150)
    F = "Engr Salahuddin Habibu Isah"

    def c1(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
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
        doc.spacer(8)
        doc.flow_chart([("MINE", ACC), ("RANK", SAGE), ("CAPTURE", TEAL), ("SELL", PLUM)], doc.y)
        doc.spacer(6)
        doc.pullquote("One seed keyword in. A full selling funnel out.")
        doc.page_break()

    def c2(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
        doc.paragraph("Every niche moves through four phases. Run all four, every time - "
                      "and the exact funnel is built right into the Workbench (/tool), "
                      "so the app itself is your roadmap.")
        colors = (ACC, SAGE, TEAL, PLUM)
        doc.spacer(10)
        n, slot = 4, doc.body_w / 4
        cw, y = slot * 0.74, doc.y - 40
        for i, (name, color) in enumerate(zip(("ATTRACT", "CONVERT", "DELIVER", "MULTIPLY"), colors)):
            x = doc.margin_x + i * slot + (slot - cw) / 2
            doc.chevron(x, y, cw, 42, color)
            doc.text(name, x + cw / 2, y + 16, 10.5, (255, 255, 255), bold=True,
                     align="center", cx=x + cw / 2)
            if i < 3:
                doc.arrow(x + cw + 4, y + 21, x + slot - 4, (160, 150, 160), width=1.6, head=5)
        doc.spacer(34)
        doc.paragraph("Attract - get people to the page (SEO niche pages, social posts, "
                      "email). Convert - turn visitors into subscribers via the opt-in "
                      "form. Deliver - send the free ebook and the 5-part email sequence. "
                      "Multiply - use analytics to double down, and add more niches.")
        doc.page_break()

    def c3(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
        doc.paragraph(
            "Mine on the Marketing Workbench (/tool) via Launch marketing, or call the "
            "mining API. Enter a broad buying seed such as 'air fryer' or 'trail "
            "camera'. pstore runs your seed through Amazon's own keyless autosuggest "
            "and product search, then reports a demand score (0-10), a saturation "
            "score (0-10) and a magnet product - the best entry offer by reviews and "
            "price.")
        doc.bullets([
            "Pick seeds with high demand and lower saturation first.",
            "Saving a niche auto-generates its review page and auto-submits it to IndexNow.",
            "One seed expands into several sub-niches from autosuggest - save them all.",
        ])
        doc.page_break()

    def c4(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
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
            "To reach Google, Bing and Yandex at full speed, open the Search "
            "Engines hub (/admin/seoengines): connect Google Search Console and "
            "Yandex via OAuth, paste your Bing API key, then Fetch stats pulls real "
            "clicks and impressions per engine and Submit sitemap re-pings it. Until "
            "a console is connected, the Traffic panel shows referral-attributed page "
            "views and clicks from your own on-site beacon.")
        doc.paragraph(
            "Bing setup is key-based: paste your API key (or set PSTORE_BING_API_KEY) "
            "and press Test connection to see which sites that key already owns, then "
            "Add site to register this domain and Submit URL to push any single page "
            "for instant crawling. The Traffic-by-engine table on the hub merges each "
            "console's real 28-day clicks/impressions into the 'Console rates' column, "
            "and the Analytics page (/admin/analytics) shows the same totals (clicks, "
            "impressions, position, CTR) next to your own referrer-attributed numbers.")
        doc.paragraph(
            "Google and Yandex mirror those same buttons once connected: Test "
            "connection lists every property (Google) or host (Yandex) your token "
            "controls, Add this site registers this site's property/host, and the URL "
            "button pushes a single page - Google's Inspect URL reports the index "
            "verdict (coverage, last crawl, robots state; daily quota ~200) while "
            "Yandex' Recrawl URL queues a re-crawl of that page.")
        doc.paragraph(
            "Google snippets come from Product JSON-LD: every niche, topic and landing "
            "page emits schema.org Product markup (price, ratings and brand only when "
            "the scraped data really has them), so rich results with stars are "
            "eligible. Re-check '/admin/seo' after a change - the Schema column turns "
            "green when the emitted markup is valid.")
        doc.paragraph(
            "Own the site everywhere it is listed: claim your domain with Google, "
            "Bing, Yandex and Pinterest from the Keys page (/keys) - paste each "
            "engine's verification token (or the meta tag it gives you) and every "
            "public page emits the matching meta tag so the claim verifies.")
        doc.paragraph(
            "Prove the tags are alive, not just written: on the Search Engines hub "
            "(/admin/seoengines) pick a page and press 'Verify live tags'. pstore "
            "fetches that page exactly like a crawler and checks every social and "
            "search-engine header tag - og: and twitter: cards, canonical, robots, "
            "each ownership meta, sitemap.xml and robots.txt - then really downloads "
            "the og:image to confirm it resolves. Each tag turns green or red, so a "
            "breakage (like a share card that no longer renders) shows up instantly "
            "instead of silently hurting your previews and claims.")
        doc.paragraph(
            "When a Google console call fails, the Engines hub now says WHY and how "
            "to fix it: a 403/Permission denied maps to 'enable the Search Console "
            "API in Google Cloud → APIs & Services → Library' (and add this account "
            "as a test user while the consent screen is in Testing mode), a quota "
            "error maps to the ~200/day URL-Inspection limit, and an unknown "
            "property points at adding it via 'Add this site'. A cancelled consent "
            "shows 'consent was denied' instead of a cryptic stale-link message, so "
            "every Google API error is actionable on the page itself.")
        doc.page_break()

    def c5(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
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

    def c6(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
        doc.paragraph(
            "Every niche gets a dedicated, fully editable sales landing page at "
            "/lp/<slug>. It is the money page: it turns cold traffic (from Google, "
            "social, QR codes or emails) into subscribers and Amazon clicks. You run "
            "it entirely from the Landing Pages editor (/admin/cms) - no code.")
        doc.bullets([
            "Pick a style template in one click: sunset (warm default), clean, forest, ocean or midnight (dark). The preset re-skins the entire page instantly.",
            "Feature toggles switch the promo banner + discount code, the countdown timer, the sticky buy button and reveal animations on or off per niche.",
            "Every section has its own show/hide switch: hero, social proof, product spotlight, email gate, testimonials, urgency, guarantee, FAQ and more.",
            "Generate copy rebuilds all section text from the niche in one click; Apply preset re-skins without touching your custom copy - the two never fight.",
        ])
        doc.paragraph(
            "The page is persuasion-engineered (Suby's How to Sell Like Crazy + "
            "Cialdini's Influence): live-data social proof, scarcity counters, an "
            "honest disclosure and a reciprocity offer - a free niche PDF guide "
            "delivered the moment a visitor opts in. Email gate on means the PDF is "
            "unlocked by a short-lived token issued on subscribe; flip the PDF gate "
            "off to hand out the PDF with no email wall. Landing pages are indexed "
            "too: each /lp/<slug> appears in your sitemap with a canonical URL.")
        doc.page_break()

    def c7(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
        doc.paragraph(
            "Every subscriber enters a 5-email sequence built from the niche's top "
            "pick: (1) hook and value, (2) social proof, (3) objections, (4) soft "
            "urgency, (5) follow-up and review request. Each email carries your tagged "
            "Amazon link.")
        doc.paragraph(
            "A brand-new opt-in gets an immediate welcome email (email #1 of the "
            "sequence) the moment they subscribe: it greets them by name and attaches "
            "the niche's free PDF guide right away, so the lead magnet lands in their "
            "inbox while their interest is hot. Re-subscribers are not re-opted (and "
            "never re-emailed) - only genuinely new leads get the welcome. If SMTP "
            "isn't configured yet, the welcome is skipped silently and the mail just "
            "waits; every step is driven by the subscriber's stored sent_index, so "
            "the daily auto-send can never double-send a step.")
        doc.paragraph(
            "Email Studio (/admin/emails) is the one place to compose and send mail: "
            "pick any niche, segment (hot/warm/cold/converted/inactive) or typed "
            "address list, choose a sequence step, converted follow-up, re-engage or "
            "a fully custom subject + body, then deliver now or schedule a slot (UTC). "
            "A live preview shows exactly what lands in the inbox.")
        doc.bullets([
            "Pick recipients with the checkboxes, or type any address(es) - one per line; the recipient count updates live.",
            "The Studio's tabs organize the whole mail lifecycle: Compose, Inbox (replies), Subscribers (search, filter, unsubscribe/resubscribe/delete), Drafts (save, reopen and edit any composition) and Sent/scheduled (review or cancel scheduled mail).",
            "Switches control the tracked affiliate link, open-tracking pixel, PDF attachment, dedup and sequence progress.",
            "Dry-run first to preview the send counts without emailing anyone.",
            "Auto-send runs the 5-step sequence to every ready subscriber on your chosen UTC hours; the Email Studio page shows the last run and lets you toggle it.",
            "Replies are captured too: the Studio's Inbox tab polls your IMAP mailbox (IMAP_HOST/USER/PASSWORD, or a forwarder hitting /api/cron/inbox) and maps each reply back to the subscriber via a tagged Reply-To address, so you can read, archive, mark read or reply from the same page.",
        ])
        doc.page_break()

    def c8(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
        doc.paragraph(
            "The Ebooks page (/admin/ebooks) turns any niche into a designed PDF in "
            "one click. Free AI providers are built in (OpenCode, Mistral, NVIDIA) so "
            "it works with no budget; add an OpenAI key for higher polish.")
        doc.paragraph(
            "The Social page (/admin/social) generates a ready-to-post kit for X, "
            "Facebook, LinkedIn, Instagram, Pinterest and Threads. Each kit has a "
            "platform caption, hashtags and a tracked link with its own code, so every "
            "post's clicks are counted individually in Analytics.")
        doc.paragraph(
            "Posts can be scheduled to future peak slots under 'Schedule post kits', "
            "then pushed out two ways from the 'Bulk publishing' toolbar: 'Flush due "
            "scheduled posts' publishes exactly the ones that are due now (what the "
            "timer does automatically) — while 'Launch blitz' (the primary button) "
            "ignores the schedule and publishes EVERY queued post across all "
            "platforms immediately. That one click is the 'sell it now' moment: "
            "front-loads the entire queue just before a launch, a promo or a sale "
            "ends, so the funnel gets its full social send right away instead of "
            "trickling out. 'Publish every niche' and 'Recycle long-tail topics' sit "
            "beside them as quieter secondary buttons, so the toolbar never crowds.")
        doc.paragraph(
            "Marketing boosts on the Workbench (/tool) mint a real, UTM-tracked "
            "campaign per promo angle: Run persists it, folds in the SEM long-tail "
            "phrases, warms the lead-magnet PDF and pings IndexNow; the Social page "
            "can publish it as a live, attributed Boost post. Each boost keeps a "
            "stable link, so its clicks aggregate over time.")
        doc.page_break()

    def c9(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
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
        doc.pullquote("Fresh data protects trust - and trust protects commissions.")
        doc.page_break()

    def c10_g(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
        doc.paragraph(
            "The whole product is one page of sections - here is the complete map, "
            "so you always know where every screen lives and what it is for. Each "
            "row is a real page or route in the app.")
        rows = [
            ("Dashboard /dashboard", "every tool at a glance"),
            ("Workbench /tool", "mine, launch, boosts, funnel"),
            ("Keys /keys", "affiliate tag + endpoints"),
            ("Email Studio /admin/emails", "compose, segments, inbox"),
            ("Landing pages /admin/cms", "presets, toggles, copy"),
            ("Ebooks /admin/ebooks", "PDF lead magnet"),
            ("Analytics /admin/analytics", "clicks, views, sources"),
            ("Social /admin/social", "tracked kits, 6 platforms"),
            ("SEM /admin/sem", "long-tails + briefs"),
            ("SEO audit /admin/seo", "indexability strip"),
            ("Search engines /admin/seoengines", "GSC, Yandex, Bing"),
            ("Refresh /admin/refresh", "auto + manual re-mine"),
            ("Users & roles /admin/users", "accounts + function matrix"),
            ("Public pages /n, /lp, sitemap", "the ranked site"),
            ("Inbox (Email Studio)", "replies via tagged Reply-To"),
            ("Landing home /", "the public storefront"),
        ]
        doc.two_col(rows)
        doc.spacer(6)
        doc.pullquote("Pick a section in the nav, or type its page path straight "
                      "into the address bar.")
        doc.page_break()

    def c10(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
        doc.paragraph(
            "Watch Analytics and double down on the most-clicked products and best "
            "sources. It shows page views (leads + public site) and lead-page "
            "interactions - promo taps, countdown hits, sticky CTA clicks, PDF "
            "downloads - so you can tune the landing page itself, not just the links. "
            "Here is a busy month, purely illustrative:")
        doc.spacer(8)
        doc.bar_chart(doc.margin_x, doc.y - 105, doc.body_w, 96,
                      [4.2, 3.1, 2.4, 1.6], ["SEO", "Social", "Email", "Direct"],
                      (ACC, SAGE, TEAL, PLUM), title="Clicks by source")
        doc.spacer(8)
        doc.paragraph(
            "Use the numbers to pick your next niche: high demand, lower saturation "
            "first - and always let the honest referral data, never a guess, tell "
            "you where the next post or email goes.")
        doc.page_break()

    def c11(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
        doc.paragraph(
            "pstore runs as a small team. From the login page a colleague requests "
            "an account, signs up with their work email and receives a branded "
            "confirmation email - the account stays unverified until they click that "
            "link, which activates it and signs them straight in. Colleagues with "
            "tool access land on the Dashboard; everyone else lands on a welcome "
            "page until the owner grants roles.")
        doc.bullets([
            "Every tool belongs to one function (Idea tools, Email Studio, Social, "
            "SEO and consoles, Content, Marketing and ROI, Analytics, Keys). A role "
            "is a custom matrix of functions; a user can hold several roles and "
            "their access is the union, enforced server-side on pages and their "
            "APIs alike.",
            "Only the owner manages users and roles (/admin/users) - create or "
            "disable users, change roles, reset passwords. Disabling an account "
            "kills its sessions instantly.",
            "Team members edit their own profile on the Dashboard (display name, or "
            "a new password with the same policy rules as signup); their session "
            "survives a name change.",
            "Forgot your password? The login page emails a secure, single-use reset "
            "link (valid one hour) that never leaks whether an email has an account.",
        ])
        doc.page_break()

    def c12(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
        doc.paragraph(
            "Every send carries a unique tagged Reply-To (pstore+<subscriber-id>@"
            "yourdomain), so when a customer replies the Email Studio Inbox tab "
            "catches it. Either it polls your IMAP mailbox (IMAP_HOST / IMAP_USER / "
            "IMAP_PASSWORD) every 60 seconds, or any forwarder can POST /api/cron/"
            "inbox with your EMAIL_CRON_SECRET to trigger a pull on demand.")
        doc.bullets([
            "Each reply is linked to its subscriber: the tag in the Reply-To wins, "
            "with a from-address match as fallback.",
            "Open a message to read it, then Reply right from the studio - the "
            "answer threads (Re:) in the customer's mail client.",
            "Mark read/unread, archive or delete any message; the Studio's tabs "
            "cover the whole lifecycle: Compose, Inbox, Subscribers, Drafts and "
            "Sent (with cancel for still-scheduled mail).",
        ])
        doc.page_break()

    def c13(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
        doc.paragraph("The short, high-leverage habits that keep every niche fresh:")
        doc.bullets([
            "Set the affiliate tag and SMTP first - nothing else matters until those work.",
            "Stack traffic: SEO (compounding) + social (fast) + email (cheap) + QR and landing pages (offline).",
            "Watch Analytics and double down on the most-clicked products and best sources.",
            "Choose niches like a fund: high demand, lower saturation first.",
            "Comply always: opted-in emails, FTC/Associates disclosure, direct tagged links.",
        ])
        doc.spacer(10)
        doc.paragraph("The 30-minute run for a new niche - tick them off as you go "
                      "(the boxes really are tickable in your PDF reader):",
                      color=(150, 140, 155))
        for i, step in enumerate([
            "Pick a seed (high demand, lower saturation)",
            "Mine it, review demand and saturation, save the niche",
            "SEO audit - confirm indexable, fix any reds",
            "SEM - note the long-tails and PAA prompts",
            "Landing pages - pick a preset, set toggles, generate copy",
            "Workbench - Launch marketing",
            "Ebooks - generate the PDF",
            "Social - publish to 1-2 platforms",
            "Email Studio - dry-run, then send the first batch",
            "Refresh - confirm auto-refresh is on, paste links and QR codes",
        ]):
            doc.checkbox("run_%d" % (i + 1), step, checked=(i < 5))
        doc.spacer(14)
        doc.pullquote("Run all four phases, every time, for every niche.")
        doc.page_break()

    def c14(doc, num, label, blurb):
        doc.chapter(num, label, blurb)
        doc.paragraph(
            "pstore was conceived, architected and built from scratch by " + F + " - "
            "one software engineer, one codebase, and not a single third-party "
            "library. The whole machine you hold in your hands is hand-written "
            "Python on the standard library: the mining engine, the search-ranked "
            "review pages, the landing-page CMS, the email studio with its 5-part "
            "sequence, the AI ebook builder, the social kits, the UTM boosts, the "
            "click analytics, the auto-refresh loop, the rate-limited security layer "
            "and this very book - drawn page by page by the app's own PDF engine.")
        doc.two_col([
            ("Role", "Founder and lead engineer"),
            ("Email", "salahuddinhabibisah@gmail.com"),
            ("Phone", "+234 803 9177 353"),
            ("Built", "pstore, end to end"),
        ])
        doc.paragraph(
            "Every feature exists because it earns its keep in the money loop: "
            "opt-in only email, honest attribution, disclosed affiliate links, and "
            "fresh pricing. If you run the loop well, the software quietly prints "
            "the smallest, tidiest commissions a machine can make.")
        doc.spacer(6)
        doc.pullquote("Built by hand, shipped daily, kept honest by one rule: "
                      "every Amazon link carries your tag.")
        doc.spacer(8)
        box_w, box_h, y = doc.body_w, 40, doc.y - 26
        doc.rect(doc.margin_x, y, box_w, box_h, (255, 247, 240), r=12)
        doc.rect(doc.margin_x, y, 5, box_h, (255, 107, 44), r=3)
        doc.text("Visit the live guide", doc.margin_x + 18, y + 24, 11.5, (45, 40, 52), bold=True)
        doc.text("pstore-gxbv.onrender.com", doc.margin_x + 18, y + 10, 10, (130, 120, 145))
        doc.uri(doc.margin_x, y, box_w, box_h, "https://pstore-gxbv.onrender.com")
        doc.spacer(30)
        doc.text("Thank you for running it.", doc.margin_x, doc.y - 12, 12, (45, 40, 52), bold=True)
        doc.page_break()

    return [
        (1, "Money machine in a box", "What pstore is, and the loop it runs", c1),
        (2, "Run the four phases", "The mental model that drives every niche", c2),
        (3, "Step 1 - Mine a niche", "Seeds, demand, saturation", c3),
        (4, "Step 2 - Rank on the web", "SEO audit, SEM brief, site claims", c4),
        (5, "Step 3 - Capture the email", "Opt-in forms that lift open rates", c5),
        (6, "The sales landing page", "CMS page, presets and toggles", c6),
        (7, "Step 4 - Send the sequence", "Email Studio, segments, schedules", c7),
        (8, "Ebook, social and boosts", "Lead magnet, kits, UTM campaigns", c8),
        (9, "Launch and keep data fresh", "One click to whole funnel", c9),
        (10, "Every tool at a glance", "The complete map of sections", c10_g),
        (11, "Read the numbers", "Analytics, page views, interactions", c10),
        (12, "Team access and roles", "Accounts, confirmation, permissions", c11),
        (13, "Inbox and conversations", "Replies, threads, IMAP", c12),
        (14, "Highest-form playbook", "Habits, checklist, tick-box run", c13),
        (15, "About the founder", "The engineer who built it from scratch", c14),
    ]


def build_pdf():
    """Two passes: first records every chapter's start page, then we draw the
    cover, a clickable Contents, and the chapters (identical rendering, so the
    page numbers and internal links stay exact)."""
    plan = {}
    _probe = pdfgen.Pdf(accent=_ACCENT, bg=_BG)
    for num, label, blurb, fn in _chapters():
        plan[num] = _probe.current_page()
        fn(_probe, num, label, blurb)
    doc = pdfgen.Pdf(accent=_ACCENT, bg=_BG)
    doc.cover(
        "The pstore User Guide",
        "A step-by-step book for running the software at its highest form.",
        kicker="OWNER MANUAL",
        owner="Engr Salahuddin Habibu Isah",
        site="Built from scratch by the founder - a free guide from pstore",
    )
    doc.page_break()
    doc.heading("Contents")
    doc.spacer(6)
    for num, label, blurb, fn in _chapters():
        start = plan[num] + 2  # the cover and this Contents page shift chapters
        doc._toc_row("%d" % num, label, start + 1, start)
    doc.page_break()
    for num, label, blurb, fn in _chapters():
        fn(doc, num, label, blurb)
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
<p class="hint" style="margin-top:10px">The companion PDF is now a real 6×9" book: styled pages, clickable links and a live table of contents, diagrams and a tickable 30-minute checklist — download it and tick boxes right in your PDF reader.</p>
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
  <a href="#engine">14 · Daily content engine</a>
  <a href="#audience">15 · Audience &amp; geo</a>
  <a href="#look">16 · Look &amp; feel</a>
  <a href="#founder">17 · About the founder</a>
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
<p><b>Google re-crawls in seconds, not weeks.</b> Once GSC is connected, pstore pushes every new niche/topic page through the URL Inspection API (crawl-request) the moment it's saved — up to 190 pages a day stays under Google's quota — and re-submits your sitemap daily. The Grow page's expand buttons do the same for every page they mint, so a fresh site stops waiting on discovery.</p>
<p><b>Paid traffic gets its own affiliate tag.</b> Set <code>PSTORE_PAID_TAG</code> (env) or a <code>paid.tag</code> via the Engines hub, and any visit that lands with <code>?utm_source=</code> on a paid network (or <code>utm_campaign</code>, or a Facebook/TikTok/Google <code>…clid</code>) renders every Amazon link on that page with the paid tag instead of the organic one — paid clicks are credited and reported separately (<code>source=paid</code>) while organic links keep the default tag.</p>
<p>Prove every <b>social and search-engine header tag</b> is active, not just written: on the Engines hub pick a page and press <b>Verify live tags</b>. pstore fetches the live page like a crawler and checks each og:/twitter: card, canonical, robots, ownership meta, sitemap.xml and robots.txt — then really downloads the og:image to confirm it resolves. Every tag turns <span style="color:#1e8e3e">green</span> or <span style="color:#c0392b">red</span>, so a broken share card or a token that never reached the live head shows up instantly.</p>

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
<h3 id="engine">14 · The daily content engine</h3>
<p>The <a class="tooltag" href="/admin/opportunities">🪴 Grow</a> page now runs a <b>daily content engine</b> that builds long-tail topic pages and queues social kits <b>unattended</b> — so the site keeps compounding even on days you don't touch it.</p>
<ul class="step-list">
<li><b>Page building</b>: each loop visit picks the niche with the most click heat and builds the next long-tail topic page (<code>/n/&lt;parent&gt;/&lt;term&gt;</code>) until that niche's keyword bank is exhausted, then moves to the next strongest niche. Fresh pages are pinged to IndexNow as they're published.</li>
<li><b>Kit scheduling</b>: ranked social posts are queued across the social calendar at real peak slots — each tagged with its own UTM so clicks attribute to the post that earned them. A kit is skipped only when its exact link is already pending/published on that platform, so nothing is double-posted.</li>
<li><b>Safety caps</b>: the engine obeys hard limits every day (<i>pages per day</i>, <i>kits per day</i>) and only runs within its <i>hours</i> window — you control all of them on the engine card, plus an optional <b>only these niches</b> filter.</li>
<li>The card shows the live ticker: pages built, kits queued, last run, next run. <b>Save</b> applies the caps, <b>Run now</b> fires an immediate pass.</li>
<li>Everything is idempotent and cheap: no network, no double posts, nothing to clean up — turn it on and let it feed the machine.</li>
</ul>

<h3 id="audience">15 · Audience &amp; geo</h3>
<p>The courier beacon now records <b>where every click comes from</b>, and the SEM briefs tell you exactly who is searching — so you optimise for the actual buyer, not the keyword alone.</p>
<ul class="step-list">
<li>Every page view and Amazon click is tagged with the visitor's <b>country</b> (beacon <code>CF-IPCountry</code>) and stored per-niche, per-ASIN.</li>
<li><a class="tooltag" href="/admin/sem">🎯 SEM</a> briefs now include a <b>👥 Who is searching</b> block: audience region mix for the keyword, a named persona (e.g. <i>the weekend snack-shopper</i>) and the intent labels (browse/compare/buy) the search carries — generated deterministically from the geo data, no AI needed.</li>
<li>The <a class="tooltag" href="/admin/opportunities">🪴 Grow</a> page shows the <b>audience signal</b> card: which countries dominate your traffic and clicks, and how those reads convert, so your copy, pricing and product picks can follow the money.</li>
<li>Audience, personas and intents respect your <b>demography settings</b> (household sizes, incomes, age bands on the 🛠 <a class="tooltag" href="/tool">Workbench</a> 🌍 Market demography panel) — the profile answers read from the same settings you already keep.</li>
</ul>

<h3 id="look">16 · Look &amp; feel — template &amp; style</h3>
<p>Give every one-pager a house style without touching a single template. The <a class="tooltag" href="/admin/template">🎨 Template &amp; style</a> page is the site-wide styling console.</p>
<ul class="step-list">
<li><b>Preset looks</b>: Ocean, Forest, Coral, Violet, Mono — or keep the classic Base. Each recolors the accent, gradients, rings and accent-2 across all <code>/n/</code> pages at once.</li>
<li><b>Advanced style</b>: paste your own CSS, override the accent color, switch the font stack, or set a card radius — the page applies your overrides on top of the preset and renders it instantly on the <b>👁 Preview first niche</b> button.</li>
<li><b>Announcement banner</b>: switch on the page feature and every targeted onepager shows a slim, on-brand banner at the very top (gradient using your accent, with an optional link) — perfect for a promo, a launch or a shipping note.</li>
<li><b>Targeting</b>: apply the look to <b>All</b> niches, <b>Only these</b>, or <b>All except</b> — listing niches or keywords (e.g. <i>keto snacks, back massager for pain relief deep tissue</i>) for surgical control. The card tells you how many of your saved niches the current settings hit.</li>
<li>Untouched niches keep rendering byte-identical — styling only ever appears on pages you explicitly target.</li>
</ul>

<h3 id="founder">17 · About the founder</h3>
<p>pstore was conceived, architected and built <b>from scratch</b> by <a href="mailto:salahuddinhabibisah@gmail.com"><b>Engr Salahuddin Habibu Isah</b></a> — one software engineer, one codebase, and not a single third-party library. The whole machine is hand-written Python on the standard library: the mining engine, the search-ranked review pages, the landing-page CMS, the email studio with its 5-part sequence, the AI ebook builder, the social kits, the UTM boosts, the click analytics, the auto-refresh loop, the security layer — and this manual's PDF, drawn page by page by the app's own book-format PDF engine.</p>
<ul class="step-list">
<li><b>Role</b> — Founder and lead engineer.</li>
<li><b>Email</b> — <a href="mailto:salahuddinhabibisah@gmail.com">salahuddinhabibisah@gmail.com</a>.</li>
<li><b>Phone</b> — +234 803 9177 353.</li>
<li>Every feature exists because it earns its keep in the money loop: opt-in-only email, honest attribution, disclosed affiliate links, and fresh pricing. Run the loop well and the software quietly prints the smallest, tidiest commissions a machine can make.</li>
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
