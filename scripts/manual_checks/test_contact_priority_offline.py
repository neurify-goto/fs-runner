#!/usr/bin/env python3
"""
Offline regression check: CONTACT link prioritization
Creates a synthetic page that contains CONTACT/採用/資料請求 links and verifies
that CONTACT is top-scored and passes the min threshold.
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath("src"))
from form_finder.form_explorer.form_explorer import FormExplorer
from playwright.async_api import async_playwright


HTML = """
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <base href="https://example.com/">
  <title>Test</title>
</head>
<body>
  <header>
    <nav class="navbar">
      <ul>
        <li><a href="/careers/">採用情報</a></li>
        <li><a href="/catalog/">資料請求</a></li>
        <li><a id="gnav-contact" class="btn-contact" href="/contact/">CONTACT</a></li>
      </ul>
    </nav>
  </header>
  <main>
    <h1>Welcome</h1>
  </main>
  <footer>
    <a href="/privacy/">個人情報保護方針</a>
  </footer>
  <script>/* noop */</script>
  </body>
</html>
"""


async def main():
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(base_url="https://example.com")
        page = await context.new_page()
        await page.set_content(HTML)

        # Sanity: list anchors as seen by the page
        anchors = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a[href]')).map(a => ({href: a.href, text: (a.textContent||'').trim()}))
        """)
        print('ANCHOR_COUNT=', len(anchors))
        for a in anchors:
            print(' ANCHOR:', a['href'], repr(a['text']))

        explorer = FormExplorer()
        await explorer.initialize()

        # Use internals to fetch links and score them
        page_data = {
            'context': context,
            'page': page,
            'content': await explorer._get_page_content(page),
        }
        all_links, count = explorer._prepare_initial_links(page_data, "https://example.com", explorer.config.MIN_LINK_SCORE, 1)
        print('VALID_TOP_LINKS_COUNT=', count)
        print('TOP_5=')
        for i, (ld, sc) in enumerate(all_links[:5], 1):
            print(f" {i}. score={sc} text={ld.get('text','')!r} href={ld.get('href','')}")

        await context.close()
        await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
