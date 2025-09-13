#!/usr/bin/env python3
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath("src"))
from playwright.async_api import async_playwright
from form_finder.form_explorer.form_explorer import FormExplorer
from form_finder.form_explorer.link_scorer import LinkScorer


async def main():
    url = os.environ.get('TARGET', 'http://www.asahicop.co.jp/')
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(url, wait_until='networkidle')
        exp = FormExplorer()
        await exp.initialize()
        content = await exp._get_page_content(page)
        links = content['links']
        ls = LinkScorer()
        valid = ls.filter_valid_links(links, url)
        print('VALID=', len(valid))
        scored = ls.score_links(valid, url)
        print('SCORED_COUNT=', len(scored))
        for link, score in scored:
            if 'contact' in link.get('href','').lower() or '問い合わせ' in (link.get('text','')+link.get('alt','')):
                print('CONTACT_CAND:', score, link.get('text'), link.get('href'))
        await ctx.close()
        await browser.close()


if __name__ == '__main__':
    asyncio.run(main())

