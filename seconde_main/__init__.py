"""Seconde Main: a second-hand listings monitor for French-speaking Switzerland.

Layout, roughly in the order data flows through it:

    sources/   one adapter per marketplace, all returning the same listing dict
    crawler    polite HTTP for sites that allow robots (robots.txt, delays, honest UA)
    browser    a real logged-in browser, for Facebook Marketplace only
    mailbox    reads alert e-mails from sites that refuse crawling
    engine/    the scan loop: fetch, dedupe, store, match against searches
    ai/        optional LLM layer: relevance scoring, translation, the assistant
    geo        offline geocoding and travel time in minutes
    db         SQLite schema and helpers; nothing is ever hard-deleted
    web/       Flask routes, one module per area of the UI
"""
