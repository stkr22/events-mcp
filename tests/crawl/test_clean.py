from events_mcp.crawl.clean import apply_ignore_patterns, content_hash, html_to_markdown

CARDS = "".join(
    f'<article><h3><a href="/e/{i}">Ausstellung Nummer {i}</a></h3>'
    f"<p>Vom {i}. Dezember bis 31. Januar im Haupthaus.</p></article>"
    for i in range(1, 9)
)


def test_overlay_dialog_is_dropped_but_content_stays() -> None:
    html = (
        '<html><body class="cookie-consent-active"><div id="cookie-banner"><h2>Datenschutz-Einstellungen</h2>'
        "<p>Wir verwenden Cookies und andere Technologien für Analysen.</p></div>"
        f"<main><h1>Programm</h1>{CARDS}</main></body></html>"
    )
    text = html_to_markdown(html)
    assert "Datenschutz" not in text and "Cookies" not in text
    assert "Ausstellung Nummer 8" in text


def test_card_listings_survive_when_extraction_would_keep_only_an_intro() -> None:
    html = (
        f"<html><body><main><p>{'Willkommen in unserem Haus. ' * 6}</p><section>{CARDS}</section></main></body></html>"
    )
    text = html_to_markdown(html)
    assert all(f"Ausstellung Nummer {i}" in text for i in range(1, 9))


def test_ignore_patterns_make_the_hash_stable() -> None:
    a = apply_ignore_patterns("Programm\nBesucher heute: 1234\nKonzert", [r"^Besucher heute"])
    b = apply_ignore_patterns("Programm\nBesucher heute: 9999\nKonzert", [r"^Besucher heute"])
    assert content_hash(a) == content_hash(b)
