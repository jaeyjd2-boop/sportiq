"""Collect every URL behind /sitemap.xml (a sitemap index since 2026-11-25)."""
import re

SITE = "https://billnojay.com"


def all_locs(client):
    index = client.get("/sitemap.xml").text
    assert "<sitemapindex" in index
    locs = []
    for sub in re.findall(r"<loc>(.*?)</loc>", index):
        locs += re.findall(r"<loc>(.*?)</loc>", client.get(sub[len(SITE):]).text)
    return locs
