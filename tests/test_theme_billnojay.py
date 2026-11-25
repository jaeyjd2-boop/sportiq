"""billnojay.com colours: dark grey + crimson (2026-11-25).

Scratch SQLite only; no network.
"""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, Response  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import sportiq_theme as th  # noqa: E402
from app.main import app  # noqa: E402

CLIENT = TestClient(app)
ROOT = Path(th.__file__).parent
OLD_BRAND = ("#10b981", "#34d399", "#059669", "#6ee7b7", "#14b8a6", "#0f172a", "#020617", "#1e293b")


def lum(hexc):
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = th._rgb(hexc)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class RemapTests(unittest.TestCase):
    def test_brand_greens_and_slate_become_grey_crimson(self):
        src = "a{color:#10B981} b{background:#020617;border:1px solid #1e293b} c{box-shadow:0 0 8px rgba(16, 185, 129, .4)}"
        out = th.remap(src)
        self.assertIn("#ff4d5e", out)
        self.assertIn("#1a1d23", out)
        self.assertIn("#2c3038", out)
        self.assertIn("rgba(255, 77, 94, .4)", out)
        for old in OLD_BRAND:
            self.assertNotIn(old, out.lower())

    def test_meaningful_colours_are_kept(self):
        src = "#4ade80 #22c55e #f87171 #ef4444 #38bdf8 #fff #10b9810 rgba(1,2,3,.5)"
        self.assertEqual(th.remap(src), src)

    def test_tailwind_config_in_html_matches_module(self):
        html = (ROOT / "sportiq_community.html").read_text(encoding="utf-8")
        m = re.search(r"colors: (\{.*?\}\}),\n", html)
        self.assertIsNotNone(m)
        self.assertEqual(json.loads(m.group(1)), th.TAILWIND_COLORS)
        for name, shades in th.TAILWIND_COLORS.items():
            for shade, new in shades.items():
                self.assertRegex(new, r"^#[0-9a-f]{6}$", f"{name}-{shade}")

    def test_contrast(self):
        self.assertGreaterEqual(contrast(th.BG, th.ACCENT), 4.5)        # dark text on crimson buttons
        self.assertGreaterEqual(contrast(th.TEXT, th.BG), 7.0)
        self.assertGreaterEqual(contrast(th.TEXT_SUB, th.BG), 4.5)
        self.assertGreaterEqual(contrast(th.ACCENT, th.BG), 4.5)        # crimson links/labels
        self.assertGreaterEqual(contrast(th.ACCENT_2, th.BG), 7.0)
        self.assertGreaterEqual(contrast(th.TAILWIND_COLORS["emerald"]["400"], th.BG), 4.5)
        self.assertGreaterEqual(contrast(th.TAILWIND_COLORS["slate"]["400"], th.BG), 4.5)
        self.assertGreaterEqual(contrast("#ffffff", th.TAILWIND_COLORS["emerald"]["600"]), 4.5)


class MiddlewareTests(unittest.TestCase):
    def test_only_html_js_css_are_rewritten(self):
        mini = FastAPI()
        mini.add_middleware(th.ThemeMiddleware)
        mini.add_api_route("/h", lambda: HTMLResponse("<b style='color:#10b981'>x</b>"))
        mini.add_api_route("/j", lambda: Response("x='#10b981'", media_type="application/javascript"))
        mini.add_api_route("/d", lambda: JSONResponse({"c": "#10b981"}))
        c = TestClient(mini)
        r = c.get("/h")
        self.assertEqual(r.text, "<b style='color:#ff4d5e'>x</b>")
        self.assertEqual(int(r.headers["content-length"]), len(r.content))
        self.assertEqual(c.get("/j").text, "x='#ff4d5e'")
        self.assertEqual(c.get("/d").json(), {"c": "#10b981"})

    def test_home_and_bundle_have_no_old_brand_colours(self):
        home = CLIENT.get("/").text.lower()
        bundle = CLIENT.get("/api/member/p0p1-upgrade-bundle").text.lower()
        for body in (home, bundle):
            for old in OLD_BRAND:
                self.assertNotIn(old, body)
            self.assertNotIn("rgba(16,185,129", body.replace(" ", ""))
        self.assertIn("#ff4d5e", bundle)


class BrandFilesTests(unittest.TestCase):
    def test_brand_routes_serve_new_files(self):
        for path, (fname, ctype) in th._FILES.items():
            r = CLIENT.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertTrue(r.headers["content-type"].startswith(ctype), path)
            self.assertEqual(r.content, (ROOT / "static" / "brand" / fname).read_bytes(), path)
        self.assertIn('fill="#ff4d5e"', CLIENT.get("/favicon.svg").text)

    def test_icons_are_crimson(self):
        from struct import unpack
        png = (ROOT / "static" / "brand" / "pwa-icon-512.png").read_bytes()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(unpack("!II", png[16:24]), (512, 512))
        try:
            from PIL import Image
        except ImportError:
            return
        img = Image.open(ROOT / "static" / "brand" / "pwa-icon-512.png").convert("RGB")
        self.assertEqual(img.getpixel((60, 256)), th._rgb(th.ACCENT))   # tile edge
        self.assertEqual(img.getpixel((5, 5)), th._rgb(th.BG))

    def test_head_manifest_and_og(self):
        home = CLIENT.get("/").text
        head = home[: home.find("</head>")]
        self.assertIn('<link rel="icon" href="/favicon.svg" type="image/svg+xml" />', head)
        self.assertEqual(set(re.findall(r'name="theme-color" content="([^"]+)"', head)), {th.THEME_COLOR})
        self.assertIn(f"og-cover.png?v={th.OG_VERSION}", head)
        self.assertNotIn("og-cover.png?v=27", head)
        m = CLIENT.get("/manifest.webmanifest").json()
        self.assertEqual(m["theme_color"], th.THEME_COLOR)
        self.assertEqual(m["background_color"], th.BG)
        page = CLIENT.get("/standings").text
        self.assertIn("/favicon.svg", page)
        self.assertIn(f"og-cover.png?v={th.OG_VERSION}", page)
        self.assertIn("background:#1a1d23", page)


class MeaningKeptTests(unittest.TestCase):
    def test_up_hit_and_winner_colours(self):
        html = (ROOT / "sportiq_community.html").read_text(encoding="utf-8")
        self.assertIn("""sWith('▲')) return '<span class="rounded-md border border-green-500/30 bg-green-500/15""", html)
        self.assertIn("""sWith('▼')) return '<span class="rounded-md border border-rose-500/30""", html)
        dcui = (ROOT / "sportiq_datacenter_ui.js").read_text(encoding="utf-8")
        self.assertIn("r.is_hit?'#4ade80':'#f87171'", dcui)
        lounge = (ROOT / "sportiq_lounge_router.py").read_text(encoding="utf-8")
        self.assertIn('(hWin ? "#ffd166" : "#f8fafc")', lounge)

    def test_header_tagline_uses_role_terms(self):
        mu = (ROOT / "sportiq_member_upgrade.py").read_text(encoding="utf-8")
        self.assertIn("<span class='sq-brand-tagline-accent'>라이브스코어</span>", mu)
        self.assertNotIn("<span>⚡ 실시간 분석</span>", mu)


if __name__ == "__main__":
    unittest.main()
