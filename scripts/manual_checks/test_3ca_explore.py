#!/usr/bin/env python3
"""
Manual integration check for http->https redirect and contact form discovery
Target: http://www.3ca.co.jp

Runs FormExplorer against the site and prints the discovered form_url and
observed final page URL scheme. Launches GUI if available, otherwise
falls back to headless mode automatically.
"""

import asyncio
import logging
import os
from urllib.parse import urlparse

from playwright.async_api import async_playwright

import sys
sys.path.insert(0, os.path.abspath("src"))
from form_finder.form_explorer.form_explorer import FormExplorer


async def main():
    # Verbose logging to see scoring and selection details
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    target = os.environ.get('TEST_TARGET_URL', 'http://www.3ca.co.jp')

    # Prefer GUI, but fall back to headless if environment disallows GUI
    # Default to headless in constrained environments; allow override
    headless = True if os.environ.get('HEADLESS', '').lower() != 'false' else False

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless, slow_mo=0)

            explorer = FormExplorer()
            await explorer.initialize()

            form_url, pages_visited, step = await explorer.explore_site_for_forms(
                browser, target, record_id=9999
            )

            # Open a page to read final URL directly for scheme check
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(target, wait_until='load')
            final_url = page.url

            print('FORM_URL=', form_url)
            print('PAGES_VISITED=', pages_visited)
            print('SUCCESS_STEP=', step)
            print('FINAL_URL=', final_url)

            fu = form_url or ''
            fs = urlparse(fu).scheme if fu else ''
            print('FORM_URL_SCHEME=', fs)
            print('FINAL_URL_SCHEME=', urlparse(final_url).scheme)

            # Optional deep inspection of top-page link scoring
            if os.environ.get('DEEP_INSPECT', '').lower() == 'true':
                try:
                    ctx = await browser.new_context()
                    page2 = await ctx.new_page()
                    await page2.goto(target, wait_until='domcontentloaded')
                    # Reuse explorer internals to fetch links and score
                    page_data = {
                        'context': ctx,
                        'page': page2,
                        'content': await explorer._get_page_content(page2),
                    }
                    all_links, count = explorer._prepare_initial_links(page_data, target, explorer.config.MIN_LINK_SCORE, 9999)
                    print('TOP_LINKS=')
                    for i, (ld, sc) in enumerate(all_links[:10], 1):
                        print(f" {i}. score={sc} text={ld.get('text','')!r} href={ld.get('href','')}")
                    await ctx.close()
                except Exception as ie:
                    print('DEEP_INSPECT_ERROR=', repr(ie))

            await context.close()
            await browser.close()

    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    asyncio.run(main())
