import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wineasio_gui import pipewire


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    @mock.patch.object(pipewire, "graph_rate", return_value=48000)
    def test_buffer_round_trip(self, _):
        conf = self.dir / "jack.conf.d" / "60-wineasio-gui.conf"
        self.assertIsNone(pipewire.buffer_size(conf))
        pipewire.set_buffer_size(128, conf)
        self.assertEqual(pipewire.buffer_size(conf), 128)
        text = conf.read_text()
        self.assertIn("node.latency = 128/48000", text)
        self.assertIn('application.process.binary = "~^wine(64)?(-preloader)?$"', text)
        pipewire.set_buffer_size(None, conf)
        self.assertFalse(conf.exists())

    def test_rate_round_trip(self):
        conf = self.dir / "pipewire.conf.d" / "60-wineasio-gui-rate.conf"
        pipewire.set_sample_rate(44100, conf, live=False)
        self.assertEqual(pipewire.sample_rate(conf), 44100)
        self.assertIn("default.clock.allowed-rates = [ 44100 ]", conf.read_text())
        pipewire.set_sample_rate(None, conf, live=False)
        self.assertIsNone(pipewire.sample_rate(conf))

    def test_wine_binaries_match(self):
        for name in ("wine-preloader", "wine64-preloader", "wine", "wine64"):
            self.assertTrue(pipewire.WINE_BINARY_RE.match(name), name)
        for name in ("winecfg", "firefox", "pipewire"):
            self.assertFalse(pipewire.WINE_BINARY_RE.match(name), name)

    def test_latency(self):
        self.assertAlmostEqual(pipewire.latency_ms(256, 48000), 5.333, places=2)


if __name__ == "__main__":
    unittest.main()
