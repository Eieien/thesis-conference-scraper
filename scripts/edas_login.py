"""Log in to EDAS once and save the session cookies for the scraper.

Usage:  uv run python -m scripts.edas_login

A browser window opens. If EDAS_USERNAME and EDAS_PASSWORD are set in .env, the login form is
filled in and submitted for you; otherwise (or if that fails) log in by hand, then come back to
this terminal and press Enter. Only the session cookies go to data/edas_state.json.
Re-run this whenever the EDAS adapter reports that the session expired.
"""

import asyncio
import contextlib

from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth

from app.config import Settings, get_settings
from app.scrapers.edas import _looks_logged_out


async def _auto_login(page: Page, settings: Settings) -> bool:
    """Fill the login form from .env. Returns False if there is nothing to fill or it failed.

    The selectors are generic (first text/email input + the password input in the same form)
    because the EDAS login markup hasn't been inspected yet.
    """
    if not (settings.edas_username and settings.edas_password):
        return False
    password = page.locator("input[type=password]").first
    if not await password.count():
        print("No password field on the EDAS start page; can't log in automatically.")
        return False
    form = page.locator("form").filter(has=password).first
    username = form.locator("input[type=text], input[type=email], input:not([type])").first
    if not await username.count():
        print("Couldn't find the username field; can't log in automatically.")
        return False
    await username.fill(settings.edas_username)
    await password.fill(settings.edas_password.get_secret_value())
    start_url = page.url
    await password.press("Enter")
    # EDAS pages keep polling, so "networkidle" never fires; wait for the page to change instead.
    # Some logins stay on the same URL; the list-page check in main() decides either way.
    with contextlib.suppress(PlaywrightTimeoutError):
        await page.wait_for_url(lambda url: url != start_url, timeout=15_000)
    await page.wait_for_load_state("load")
    return True


async def main() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(settings.edas_base_url)

        if await _auto_login(page, settings):
            await page.goto(settings.edas_list_url)
            if _looks_logged_out(await page.content()):
                print("Automatic login didn't work (wrong credentials or a different form).")
            else:
                await context.storage_state(path=str(settings.edas_state_path))
                print(f"Logged in from .env. Saved session to {settings.edas_state_path}")
                await browser.close()
                return
            await page.goto(settings.edas_base_url)

        print(
            "\nLog in to EDAS in the browser window, then press Enter here to save the session..."
        )
        await asyncio.get_running_loop().run_in_executor(None, input)

        await page.goto(settings.edas_list_url)
        if _looks_logged_out(await page.content()):
            print("Still looks logged out (password field on the list page). Nothing saved.")
        else:
            await context.storage_state(path=str(settings.edas_state_path))
            print(f"Saved session to {settings.edas_state_path}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
