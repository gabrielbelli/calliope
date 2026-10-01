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

    for name, panel in tabs:
        tab = page.locator(f"[role=tab][aria-controls='{panel}']")
        tab.click()
        page.locator(f"#{panel}").wait_for(state="visible")
        assert tab.get_attribute("aria-selected") == "true", f"{name} did not select itself"
        hidden_others = page.locator(f"[role=tabpanel]:not(#{panel})").evaluate_all(
            "els => els.every(e => e.hidden || getComputedStyle(e).display === 'none')")
        assert hidden_others, f"another panel stayed visible beside {name}"
        # The dock's spring and the panel's entrance run on requestAnimationFrame,
        # which Playwright's animations="disabled" cannot stop; this is longer
        # than either.
        page.wait_for_timeout(700)
        screenshot(f"smoke-{form}-{scheme}-{name}", target=page)

    assert not browser_log.errors, f"uncaught errors on the page: {browser_log.errors}"
    assert not browser_log.bad_responses(), f"missing routes or server errors: {browser_log.bad_responses()}"
    broken = [f for f in browser_log.failed if "ERR_ABORTED" not in (f["error"] or "")]
    assert not broken, f"requests that failed outright: {broken}"
