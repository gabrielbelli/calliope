"""The page loads through the gateway, every tab opens its own panel, and each
one is photographed at desktop and phone size, in light and in dark.

The tabs are read off the page rather than listed here: whatever the tab bar
holds, each [role=tab] must select itself and show the panel its
aria-controls names. That is the WAI-ARIA contract the page already keeps, and
it survives the tabs being renamed, reordered or given paths of their own.
"""

from __future__ import annotations

import pytest


def tabs_of(page) -> list[tuple[str, str]]:
    """(the tab's own name, the id of the panel it controls), in order."""
    return page.locator("[role=tab]").evaluate_all(
        "els => els.map(e => [e.dataset.tab || e.id || e.textContent.trim(), e.getAttribute('aria-controls')])")


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("form", ["desktop", "mobile"])
def test_every_tab_opens_its_own_panel_and_is_photographed(new_page, goto, screenshot, browser_log, form, scheme):
    page = new_page(form, scheme=scheme)
    goto("/ui", target=page)
    page.locator("[role=tab]").first.wait_for(state="visible")
    tabs = tabs_of(page)
    assert len(tabs) >= 5, f"expected the five sections, found {tabs}"
    tops: list[int] = []

    for name, panel in tabs:
        tab = page.locator(f"[role=tab][aria-controls='{panel}']")
        tab.click()
        page.locator(f"#{panel}").wait_for(state="visible")
        assert tab.get_attribute("aria-selected") == "true", f"{name} did not select itself"
        hidden_others = page.locator(f"[role=tabpanel]:not(#{panel})").evaluate_all(
            "els => els.every(e => e.hidden || getComputedStyle(e).display === 'none')")
        assert hidden_others, f"another panel stayed visible beside {name}"
        # The panel's entrance and the dock's pill are still moving just after
        # the click, and the measurements below read the page at rest; this is
        # longer than either.
        page.wait_for_timeout(700)
        # The masthead names the tab; a heading in the panel that says it
        # again is the same word twice on one screen. Read off the rendered
        # page, so a heading drawn by script is caught as well as markup, and
        # a disclosure's summary is a heading too ("Expert: transcription"
        # under "Transcribe" was one). A row's summary is its content -- a
        # job's text, a satellite's name -- and may say anything. A legend in a
        # wake word's row is not content: it is the page's own label, drawn
        # once per word, so the rows of #wwlist are read for legends too.
        word = page.locator("#word").inner_text().strip().lower()
        forms = [word] + (["transcription"] if word == "transcribe" else [])
        said_again = page.locator(
            f"#{panel} :is(h2, h3, summary, legend):not(:is(#joblist, #satellitelist, #wwlist) *),"
            f" #{panel} #wwlist legend").evaluate_all(
            "(els, forms) => els.filter(e => e.checkVisibility()"
            " && forms.some(f => e.textContent.toLowerCase().includes(f)))"
            ".map(e => e.textContent.trim())", forms)
        assert not said_again, f"{name} says its own name again under the masthead: {said_again}"
        # And the first card sits under the title that names it, not halfway
        # down the viewport: an auto margin used to open about 250px between
        # them on a short panel.
        gap = page.evaluate("""panel => {
          const card = [...document.querySelectorAll(`#${panel} > .card`)].find(c => c.checkVisibility());
          return card.getBoundingClientRect().top - document.getElementById("tagline").getBoundingClientRect().bottom;
        }""", panel)
        assert 0 <= gap <= 48, f"{name}: {gap:.0f}px between the title and the first card"
        tops.append(round(page.evaluate(
            "panel => [...document.querySelectorAll(`#${panel} > .card`)].find(c => c.checkVisibility())"
            ".getBoundingClientRect().top", panel)))
        screenshot(f"smoke-{form}-{scheme}-{name}", target=page)

    # Where every tagline fits on one line, the card starts at the same height
    # on every tab, as the masthead does; a phone wraps the longer taglines.
    if form == "desktop":
        assert len(set(tops)) == 1, f"the first card moves between tabs: {tops}"

    assert not browser_log.errors, f"uncaught errors on the page: {browser_log.errors}"
    assert not browser_log.bad_responses(), f"missing routes or server errors: {browser_log.bad_responses()}"
    broken = [f for f in browser_log.failed if "ERR_ABORTED" not in (f["error"] or "")]
    assert not broken, f"requests that failed outright: {broken}"
