"""Match-modal head-to-head (h2h) for WKBL and V-League from the leagues' own schedules.

Fixtures are copies of the real responses on 2026-11-28:
- user-api.kovo.co.kr /stat/game-schedule?seasonCode=023&leagueCode=201
  (trimmed to games up to 2026-12-31; the real payload runs to April 2027)
- wkbl.or.kr /game/sch/schedule1.asp (current month, 2026-11) and ?season_gu=047&gun=1&ym=202612
KBL is not connected yet, so its h2h is left out rather than built from the old hardcoded calendar.
No network: _official_http_v4700 is patched with a fake router.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from app import sportiq_lounge_router as lr  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "kr_official"
KOVO_SEASONS = (FIX / "kovo_season_list.json").read_text(encoding="utf-8")
KOVO_RANK = (FIX / "kovo_team_rank.json").read_text(encoding="utf-8")
KOVO_SCHED = (FIX / "kovo_game_schedule.json").read_text(encoding="utf-8")
KBL_RANK = (FIX / "kbl_league_rank_team.json").read_text(encoding="utf-8")
WKBL_AJAX = (FIX / "wkbl_ajax_team_rank.html").read_text(encoding="utf-8")
WKBL_NOV = (FIX / "wkbl_schedule_202611.html").read_text(encoding="utf-8")
WKBL_DEC = (FIX / "wkbl_schedule_202612.html").read_text(encoding="utf-8")
WKBL_RANK_PAGE = "<script>$(function() {\n\tfnTeamRank('047', '2026-2027', '1');\n});</script>"
TODAY = "2026-11-28"
WKBL_SCHED = "https://www.wkbl.or.kr/game/sch/schedule1.asp"


def fake_http(url, data=None, headers=None, timeout=4.0):
    if url == "https://api.kbl.or.kr/league/rank/team":
        return KBL_RANK
    if url == "https://www.wkbl.or.kr/game/team_rank.asp":
        return WKBL_RANK_PAGE
    if url == "https://www.wkbl.or.kr/game/ajax/ajax_team_rank.asp":
        return WKBL_AJAX
    if url == WKBL_SCHED:
        return WKBL_NOV
    if url == WKBL_SCHED + "?season_gu=047&gun=1&ym=202612":
        return WKBL_DEC
    if url.startswith("https://user-api.kovo.co.kr/stat/season-list"):
        return KOVO_SEASONS
    if url == "https://user-api.kovo.co.kr/stat/league/team-rank?seasonCode=023&leagueCode=201":
        return KOVO_RANK
    if url == "https://user-api.kovo.co.kr/stat/game-schedule?seasonCode=023&leagueCode=201":
        return KOVO_SCHED
    return ""   # WKBL Jan-Mar 2027 pages: not in the fixtures (future months are best-effort)


def reset():
    lr._DOM_STANDINGS_SSR_CACHE_V4624.clear()
    lr._OFFICIAL_KR_FAIL_AT_V4700.clear()
    lr._KR_SCHED_CACHE_V4710.clear()
    lr._KR_SCHED_FAIL_AT_V4710.clear()


def build(code, home="", away="", http=fake_http, calls=None):
    def router(url, *a, **k):
        if calls is not None:
            calls.append(url)
        return http(url, *a, **k)

    with patch.object(lr, "_official_http_v4700", side_effect=router), \
            patch.object(lr, "_http_fetch_text_v4616", return_value=""), \
            patch.object(lr, "_kst_today_ymd_v4710", return_value=TODAY):
        return lr._build_domestic_bball_standings_v341(code, home, away)


class KovoH2hTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_women_h2h_from_official_results(self):
        h2h = build("VLEAGUE_W", "흥국생명", "GS칼텍스")["h2h"]
        self.assertIs(h2h["is_sample"], False)
        self.assertEqual(h2h["source"], "KOVO 공식 일정·결과")
        self.assertEqual((h2h["home_wins"], h2h["away_wins"]), (0, 2))
        self.assertEqual(h2h["summary"], "올 시즌 맞대결: GS칼텍스 2승 0패 우위")
        self.assertEqual(h2h["past_games"], [
            "11.19(목) 흥국생명 1 : 3 GS칼텍스 (인천삼산월드체육관)",
            "11.27(금) 흥국생명 0 : 3 GS칼텍스 (인천삼산월드체육관)",
        ])
        self.assertEqual(h2h["home_last"], "11.27 vs GS칼텍스 (0:3 패)")
        self.assertEqual(h2h["next_rematch"], "12.29 (화) 19:00 GS칼텍스 vs 흥국생명 · 서울장충체육관")

    def test_men_h2h_and_unplayed_games_ignored(self):
        h2h = build("VLEAGUE_M", "대한항공", "KB손해보험")["h2h"]
        self.assertIn("11.27(금) 대한항공 3 : 1 KB손해보험 (인천계양체육관)", h2h["past_games"])
        self.assertEqual(h2h["home_wins"] + h2h["away_wins"], len(h2h["past_games"]))
        self.assertTrue(h2h["next_rematch"] > "11.28")

    def test_soop_alias(self):
        # Old name 페퍼저축은행 and KOVO's short name SOOP are the same club.
        h2h = build("VLEAGUE_W", "페퍼저축은행", "한국도로공사")["h2h"]
        self.assertEqual(h2h["home"], "SOOP 수퍼스")
        self.assertEqual(h2h["past_games"], ["11.05(목) SOOP 수퍼스 1 : 3 한국도로공사 (숲 스타디움)"])
        self.assertTrue(h2h["next_rematch"].startswith("11.29 (일) 16:00 SOOP 수퍼스 vs 한국도로공사"))

    def test_one_schedule_call_serves_both_genders(self):
        calls = []
        build("VLEAGUE_M", "대한항공", "현대캐피탈", calls=calls)
        build("VLEAGUE_W", "흥국생명", "GS칼텍스", calls=calls)
        self.assertEqual(len([c for c in calls if "game-schedule" in c]), 1)

    def test_failure_hides_h2h_and_backs_off(self):
        calls = []

        def no_schedule(url, *a, **k):
            return "" if "game-schedule" in url else fake_http(url, *a, **k)

        for _ in range(3):
            res = build("VLEAGUE_W", "흥국생명", "GS칼텍스", http=no_schedule, calls=calls)
            self.assertNotIn("h2h", res)
            self.assertIs(res["is_sample"], False)       # standings still live
        self.assertEqual(len([c for c in calls if "game-schedule" in c]), 2)   # 2 candidate seasons, once

    def test_no_pair_no_schedule_fetch(self):
        calls = []
        res = build("VLEAGUE_M", calls=calls)
        self.assertNotIn("h2h", res)
        self.assertFalse([c for c in calls if "game-schedule" in c])


class WkblH2hTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_h2h_from_official_calendar(self):
        h2h = build("WKBL", "부산 BNK 썸", "삼성생명")["h2h"]
        self.assertEqual(h2h["source"], "WKBL 공식 일정·결과")
        self.assertEqual((h2h["home"], h2h["away"]), ("BNK썸", "삼성생명"))
        self.assertEqual(h2h["past_games"], ["11.15(일) 삼성생명 69 : 63 BNK썸"])
        self.assertEqual((h2h["home_wins"], h2h["away_wins"]), (0, 1))
        self.assertEqual(h2h["next_rematch"], "11.29 (일) BNK썸 vs 삼성생명")
        self.assertIs(h2h["is_sample"], False)

    def test_next_rematch_from_a_later_month(self):
        h2h = build("WKBL", "우리은행", "신한은행")["h2h"]
        # 11.30 is the next one; the December page is fetched too.
        self.assertEqual(h2h["next_rematch"], "11.30 (월) 우리은행 vs 신한은행")
        games = lr._parse_wkbl_schedule_month_v4710(WKBL_DEC, "202612")
        self.assertEqual(len(games), 25)
        self.assertTrue(all(g["hs"] is None for g in games))

    def test_month_page_must_match(self):
        # A page that ignored the ym parameter (shows November) must not be read as December.
        self.assertEqual(lr._parse_wkbl_schedule_month_v4710(WKBL_NOV, "202612"), [])
        nov = lr._parse_wkbl_schedule_month_v4710(WKBL_NOV, "202611")
        self.assertEqual(nov[0], {"date": "2026-11-01", "time": "", "home": "KB스타즈", "away": "BNK썸",
                                  "hs": 53, "as": 82, "venue": ""})
        self.assertEqual(sum(1 for g in nov if g["hs"] is not None), 22)

    def test_missing_played_month_hides_h2h(self):
        # Pretend it is already December: the December page has no results, so the record would be wrong.
        def no_dec(url, *a, **k):
            return "" if url.endswith("ym=202612") else fake_http(url, *a, **k)

        with patch.object(lr, "_official_http_v4700", side_effect=no_dec), \
                patch.object(lr, "_kst_today_ymd_v4710", return_value="2026-12-05"):
            games, _ = lr._fetch_official_kr_schedule_v4710("WKBL")
        self.assertIsNone(games)


class KblAndCalendarTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_kbl_h2h_not_shown(self):
        calls = []
        res = build("KBL", "서울 SK", "원주 DB", calls=calls)
        self.assertNotIn("h2h", res)
        self.assertEqual(calls, ["https://api.kbl.or.kr/league/rank/team"])   # standings only

    def test_hardcoded_calendar_not_used(self):
        poisoned = {TODAY: [["VLEAGUE_W", "14:00", "흥국생명", "GS칼텍스", 3, 0, "가짜 체육관"]]}
        with patch.object(lr, "_OFFICIAL_KOVO_WKBL_SEASON_2627_V4622", poisoned):
            h2h = build("VLEAGUE_W", "흥국생명", "GS칼텍스")["h2h"]
        self.assertFalse(any("가짜" in g for g in h2h["past_games"]))
        self.assertEqual(h2h["away_wins"], 2)
        self.assertFalse(hasattr(lr, "_compute_club_form_and_h2h_from_cal_v4624"))

    def test_modal_skips_sample_h2h(self):
        src = Path(lr.__file__).read_text(encoding="utf-8")
        self.assertIn("if (activeH2h && activeH2h.is_sample === true) activeH2h = null;", src)


if __name__ == "__main__":
    unittest.main()
