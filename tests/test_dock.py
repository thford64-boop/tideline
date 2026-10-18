"""Panel layouts as data: docking, splitting, floating, closing and
reopening panels, and loading saved layouts safely. No GTK."""

import unittest

from tideline import dock


class Layouts(unittest.TestCase):
    def setUp(self):
        self.state = dock.default("editing")

    def test_every_panel_has_a_place_in_every_workspace(self):
        for ws in dock.DEFAULTS:
            self.assertEqual(sorted(dock.all_panels(dock.default(ws))), sorted(dock.PANELS), ws)
        self.assertEqual(dock.leaf_of(dock.default("color"), "scopes")["active"], "scopes")

    def test_dock_beside_splits_the_target_group(self):
        out = dock.dock(self.state, "scopes", "program", "right")
        self.assertEqual(dock.leaf_of(out, "scopes")["tabs"], ["scopes"])
        self.assertEqual(dock.leaf_of(out, "program")["tabs"], ["program"])
        self.assertNotIn("scopes", dock.leaf_of(out, "project")["tabs"])
        node = out["tree"]["a"]["b"]
        self.assertEqual((node["split"], node["a"]["tabs"], node["b"]["tabs"]), ("h", ["program"], ["scopes"]))
        self.assertEqual(sorted(dock.all_panels(out)), sorted(dock.PANELS))
        self.assertIn("scopes", dock.leaf_of(self.state, "project")["tabs"])          # the input is untouched
        top = dock.dock(self.state, "mixer", "timeline", "top")["tree"]["b"]["b"]
        self.assertEqual((top["split"], top["a"]["tabs"]), ("v", ["mixer"]))

    def test_dock_into_a_group_and_collapse_empty_ones(self):
        out = dock.dock(self.state, "program", "timeline", "center")
        self.assertEqual(dock.leaf_of(out, "timeline")["tabs"], ["timeline", "program"])
        self.assertEqual(dock.leaf_of(out, "program")["active"], "program")
        # The top row lost its right half: the left group takes the whole row.
        self.assertTrue(dock.is_leaf(out["tree"]["a"]))

    def test_moves_that_change_nothing(self):
        self.assertEqual(dock.dock(self.state, "program", "program", "left"), self.state)    # alone in its group
        self.assertEqual(dock.dock(self.state, "source", "mixer", "center"), self.state)     # already there
        self.assertEqual(dock.dock(self.state, "source", "nowhere", "left"), self.state)
        split = dock.dock(self.state, "source", "mixer", "bottom")
        self.assertEqual(dock.leaf_of(split, "source")["tabs"], ["source"])

    def test_float_close_and_show_again(self):
        out = dock.float_panel(self.state, "history", (500, 300))
        self.assertEqual(dock.floating_index(out, "history"), 0)
        self.assertEqual(out["floating"][0]["size"], [500, 300])
        self.assertEqual(dock.float_panel(out, "history"), out)
        out = dock.dock(out, "info", "history", "center")
        self.assertEqual(dock.panels(out["floating"][0]["tree"]), ["history", "info"])
        back = dock.dock_back(out, 0)
        self.assertEqual(back["floating"], [])
        self.assertIn("history", dock.leaf_of(back, "project")["tabs"])
        closed = dock.remove(self.state, "scopes")
        self.assertNotIn("scopes", dock.all_panels(closed))
        reopened = dock.show(closed, "scopes", "editing")
        self.assertIn("scopes", dock.leaf_of(reopened, "project")["tabs"])            # back with its partners
        self.assertEqual(dock.leaf_of(reopened, "scopes")["active"], "scopes")

    def test_split_shares_and_saved_layouts(self):
        out = dock.set_pos(self.state, "a", 0.7)
        self.assertEqual(out["tree"]["a"]["pos"], 0.7)
        self.assertEqual(dock.set_pos(self.state, "", 5)["tree"]["pos"], dock.MAX_POS)
        saved = {"tree": {"split": "h", "pos": "x", "a": {"tabs": ["program", "program", "bogus"]},
                          "b": {"tabs": ["timeline"], "active": "nope"}},
                 "floating": [{"tree": {"tabs": ["program", "scopes"]}, "size": [10, "y"]}, "junk"]}
        clean = dock.sanitize(saved, "editing")
        self.assertEqual(clean["tree"]["a"], {"tabs": ["program"], "active": "program"})
        self.assertEqual(clean["tree"]["b"]["active"], "timeline")
        self.assertEqual(clean["tree"]["pos"], 0.5)
        self.assertEqual(clean["floating"], [{"tree": {"tabs": ["scopes"], "active": "scopes"}, "size": [640, 420]}])
        self.assertEqual(dock.sanitize(None, "audio"), dock.default("audio"))
        self.assertEqual(dock.sanitize({"tree": {"tabs": ["bogus"]}}, "color"), dock.default("color"))


if __name__ == "__main__":
    unittest.main()
