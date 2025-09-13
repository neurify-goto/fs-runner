#!/usr/bin/env python3
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath("src"))
from playwright.async_api import async_playwright


KEYS = [
    'お問い合わせ', 'お問合せ', '問い合わせ', 'toiawase', 'contact', 'inquiry', 'CONTACT', 'Contact', 'Inquiry'
]


async def main():
    url = os.environ.get('TARGET', 'http://example.com')
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(url, wait_until='domcontentloaded')
        candidates = await page.evaluate(
            """
            (keys) => {
                const lc = s => (s||'').toLowerCase();
                const match = (s) => keys.some(k => lc(s).includes(lc(k)));
                const rows = [];
                document.querySelectorAll('a, button, [role="button"]').forEach(el => {
                    const text = (el.textContent||'').trim();
                    const aria = el.getAttribute('aria-label')||'';
                    const title = el.getAttribute('title')||'';
                    let alt = '';
                    const img = el.querySelector('img[alt]');
                    if (img && img.getAttribute('alt')) alt = img.getAttribute('alt');
                    const href = el.href || el.getAttribute('href') || '';
                    const onclick = el.getAttribute('onclick') || '';
                    const anyMatch = match(text) || match(aria) || match(title) || match(alt) || match(href) || match(onclick);
                    if (anyMatch) {
                        rows.push({tag: el.tagName, text, aria, title, alt, href, onclick});
                    }
                });
                return rows;
            }
            """,
            KEYS,
        )
        print(f"CANDIDATES({len(candidates)}):")
        for c in candidates:
            print(c)
        await ctx.close()
        await browser.close()


if __name__ == '__main__':
    asyncio.run(main())

