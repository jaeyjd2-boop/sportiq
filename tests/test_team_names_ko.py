"""Korean football team-name table (app/sportiq_team_names_ko.py)."""
import ast
import os
import re
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SPORTIQ_LOUNGE_DB", os.path.join(tempfile.mkdtemp(), "lounge_test.db"))

from app import sportiq_lounge_router as lr  # noqa: E402
from app.sportiq_team_names_ko import SOCCER_KO  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "app" / "sportiq_team_names_ko.py"
HANGUL = re.compile("[가-힣]")


class TableTests(unittest.TestCase):
    def test_no_duplicate_keys_in_source(self):
        tree = ast.parse(SRC.read_text(encoding="utf-8"))
        keys = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys += [k.value for k in node.keys if isinstance(k, ast.Constant)]
        dups = sorted({k for k in keys if keys.count(k) > 1})
        self.assertEqual(dups, [])
        self.assertEqual(len(keys), len(SOCCER_KO))

    def test_keys_and_values_are_clean(self):
        for k, v in SOCCER_KO.items():
            self.assertEqual(k, k.strip().lower(), k)
            self.assertTrue(v.strip(), k)
            self.assertEqual(v, v.strip(), k)
            self.assertRegex(v, HANGUL, k)

    def test_does_not_contradict_older_tables(self):
        older = {}
        for tbl in (lr._KO_SOCCER_TEAMS_V37, lr._ko_extra_team_map_v4666()):
            older.update(tbl)
        clashes = {k: (older[k], v) for k, v in SOCCER_KO.items() if k in older and older[k] != v}
        self.assertEqual(clashes, {})

    def test_ambiguous_short_names_are_left_out(self):
        for k in ("america", "nacional", "al ahli", "sparta", "red star", "municipal", "juventud",
                  "santos", "houston", "dallas", "cali", "albion"):
            self.assertNotIn(k, SOCCER_KO)


class LookupTests(unittest.TestCase):
    def test_examples_from_home_cards(self):
        self.assertEqual(lr._localize_team_ko_v37("Melbourne V", "soccer"), "멜버른 빅토리")
        self.assertEqual(lr._localize_team_ko_v37("The Cong-Viettel", "soccer"), "비엣텔 FC")
        self.assertEqual(lr._localize_team_ko_v37("Al Nassr", "soccer"), "알나스르")
        self.assertEqual(lr._localize_team_ko_v37("Uganda", "soccer"), "우간다")

    def test_exact_keys_only_and_soccer_only(self):
        # "boca" is a key; a longer unknown name containing it must stay English.
        self.assertEqual(lr._localize_team_ko_v37("Boca Unidos", "soccer"), "Boca Unidos")
        self.assertEqual(lr._localize_team_ko_v37("Hearts", "basketball"), "Hearts")
        self.assertEqual(lr._localize_team_ko_v37("America Cali", "soccer"), "America Cali")

    def test_espn_full_name_reaches_table(self):
        side = {"team": {"shortDisplayName": "Adelaide Utd", "displayName": "Adelaide United FC"}}
        name = lr._espn_side_name_v4666(side, "soccer")
        self.assertEqual(lr._localize_team_ko_v37(name, "soccer"), "애들레이드 유나이티드")


if __name__ == "__main__":
    unittest.main()
