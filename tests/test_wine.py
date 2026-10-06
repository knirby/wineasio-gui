import tempfile
import unittest
from pathlib import Path

from wineasio_gui import wine

USER_REG = """WINE REGISTRY Version 2
;; All keys relative to \\\\User\\\\S-1-5-21-0-0-0-1000

#arch=win64

[Software\\\\Wine\\\\Fonts] 1791308400
#time=1dd55b64bda740c
"LogPixels"=dword:00000060

[Software\\\\Wine\\\\WineASIO] 1791308400
#time=1dd55b64bda740c
"Autostart server"=dword:00000000
"Number of inputs"=dword:00000002

[Software\\\\Wine\\\\X11 Driver] 1791308400
"Managed"="Y"
"""


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.reg = self.dir / "user.reg"
        self.reg.write_text(USER_REG)

    def test_read(self):
        values = wine.read_key(self.reg, wine.KEY)
        self.assertEqual(values, {"autostart server": 0, "number of inputs": 2})
        settings = wine.Settings.from_values(values)
        self.assertEqual(settings.inputs, 2)
        self.assertEqual(settings.outputs, 16)  # driver default
        self.assertIs(settings.autostart_server, False)

    def test_update_keeps_other_keys(self):
        settings = wine.Settings(inputs=4, outputs=6, preferred_buffersize=256)
        wine.write_key(self.reg, wine.KEY, settings.to_values())
        text = self.reg.read_text()
        self.assertIn('"LogPixels"=dword:00000060', text)
        self.assertIn('"Managed"="Y"', text)
        self.assertEqual(text.count("[Software\\\\Wine\\\\WineASIO]"), 1)
        again = wine.Settings.from_values(wine.read_key(self.reg, wine.KEY))
        self.assertEqual(again, settings)
        self.assertTrue((self.dir / "user.reg.bak-wineasio-gui").exists())

    def test_creates_missing_key(self):
        self.reg.write_text(USER_REG.replace("WineASIO", "Other"))
        wine.write_key(self.reg, wine.KEY, wine.Settings(outputs=8).to_values())
        self.assertEqual(wine.read_key(self.reg, wine.KEY)["number of outputs"], 8)
        self.assertIn("\n[Software\\\\Wine\\\\WineASIO] ", self.reg.read_text())

    def test_environment_overrides(self):
        found = wine.environment_overrides({"WINEASIO_NUMBER_INPUTS": "4", "PATH": "/bin"})
        self.assertEqual(found, {"WINEASIO_NUMBER_INPUTS": "4"})


class DiscoverTest(unittest.TestCase):
    def test_finds_prefixes(self):
        home = Path(tempfile.mkdtemp())
        for relative in (".wine", "Wine/Prefix/music", ".local/share/bottles/bottles/Studio",
                         "Games/not-a-prefix"):
            path = home / relative
            path.mkdir(parents=True)
            if "not-a-prefix" not in relative:
                (path / "system.reg").write_text("")
                (path / "user.reg").write_text("")
                (path / "drive_c").mkdir()
        labels = [p.label for p in wine.discover(home=home)]
        self.assertIn("~/.wine", labels)
        self.assertIn("~/Wine/Prefix/music", labels)
        self.assertIn("Bottles: Studio", labels)
        self.assertEqual(len(labels), 3)


if __name__ == "__main__":
    unittest.main()
