#!/usr/bin/env python3
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath("src"))
from playwright.async_api import async_playwright
from form_finder.form_explorer.form_explorer import FormExplorer

KEYS = [
    'お問い合わせ', 'お問合せ', '問い合わせ', 'toiawase', 'contact', 'inquiry'
]


async def main():
    url = os.environ.get('TARGET', 'http://example.com')
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(url, wait_until='networkidle')
        exp = FormExplorer()
        await exp.initialize()
        content = await exp._get_page_content(page)
        links = content['links']
        print('TOTAL_LINKS=', len(links))
        def m(s):
            s = (s or '').lower()
            return any(k in s for k in KEYS)
        count = 0
        for l in links:
            hay = ' '.join([l.get('text',''), l.get('ariaLabel',''), l.get('title',''), l.get('alt',''), l.get('href','')])
            if m(hay):
                count += 1
                print('HIT:', l)
        print('HIT_COUNT=', count)
        await ctx.close()
        await browser.close()


if __name__ == '__main__':
    asyncio.run(main())

