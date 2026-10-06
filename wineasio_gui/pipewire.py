"""PipeWire's side of WineASIO: buffer size, sample rate and running clients.

Under PipeWire, WineASIO is a JACK client of pipewire-jack, so its buffer is the
quantum PipeWire grants that client and its sample rate is the graph's rate.
Neither is something the ASIO host can change, which is why they are set here.
"""

import json
import os
import re
import resource
import subprocess
from dataclasses import dataclass
from pathlib import Path

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pipewire"
BUFFER_CONF = CONFIG / "jack.conf.d" / "60-wineasio-gui.conf"
RATE_CONF = CONFIG / "pipewire.conf.d" / "60-wineasio-gui-rate.conf"

BUFFER_SIZES = [16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192]
RATES = [44100, 48000, 88200, 96000, 176400, 192000]
# Wine's loader process, whatever the build calls it
WINE_BINARY = "~^wine(64)?(-preloader)?$"
WINE_BINARY_RE = re.compile(WINE_BINARY[1:])

BUFFER_TEMPLATE = """\
# Written by wineasio-gui. Every JACK client running inside Wine (that is, WineASIO)
# asks PipeWire for this buffer: {size} samples, {ms:.1f} ms at {rate} Hz.
jack.rules = [
    {{
        matches = [ {{ application.process.binary = "{binary}" }} ]
        actions = {{ update-props = {{ node.latency = {size}/{rate} }} }}
    }}
]
"""

RATE_TEMPLATE = """\
# Written by wineasio-gui: run PipeWire's graph at {rate} Hz. WineASIO can only
# offer the graph's rate to ASIO apps, so this must match the rate your project uses.
context.properties = {{
    default.clock.rate = {rate}
    default.clock.allowed-rates = [ {rate} ]
}}
"""


def _run(*command):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def setting(name):
    """A value from PipeWire's live 'settings' metadata, or None."""
    match = re.search(r"value:'(-?\d+)'", _run("pw-metadata", "-n", "settings", "0", name))
    return int(match.group(1)) if match else None


def graph_rate():
    return setting("clock.force-rate") or setting("clock.rate") or 48000


def quantum_limits():
    return setting("clock.min-quantum") or 32, setting("clock.max-quantum") or 2048


def latency_ms(size, rate=None):
    return size * 1000 / (rate or graph_rate())


# ---------------------------------------------------------------------------
# Buffer size

def buffer_size(conf=BUFFER_CONF):
    """The configured WineASIO buffer, or None when PipeWire decides."""
    try:
        match = re.search(r"node\.latency\s*=\s*(\d+)/", conf.read_text())
        return int(match.group(1)) if match else None
    except OSError:
        return None


def set_buffer_size(size, conf=BUFFER_CONF):
    """Ask PipeWire for size samples for WineASIO clients; None removes the rule."""
    if size is None:
        conf.unlink(missing_ok=True)
        return
    rate = graph_rate()
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(BUFFER_TEMPLATE.format(size=size, rate=rate, ms=latency_ms(size, rate),
                                           binary=WINE_BINARY))


# ---------------------------------------------------------------------------
# Sample rate

def sample_rate(conf=RATE_CONF):
    """The rate wineasio-gui pinned the graph to, or None for the system default."""
    try:
        match = re.search(r"default\.clock\.rate\s*=\s*(\d+)", conf.read_text())
        return int(match.group(1)) if match else None
    except OSError:
        return None


def set_sample_rate(rate, conf=RATE_CONF, live=True):
    """Pin the graph rate (persistently, and right now); None returns to the default."""
    if rate is None:
        conf.unlink(missing_ok=True)
    else:
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(RATE_TEMPLATE.format(rate=rate))
    if live:
        _run("pw-metadata", "-n", "settings", "0", "clock.force-rate", str(rate or 0))


# ---------------------------------------------------------------------------
# Running clients

@dataclass
class Client:
    node_id: int
    name: str
    buffer: int | None
    rate: int | None
    xruns: int | None


def clients():
    """WineASIO clients PipeWire knows about right now."""
    try:
        objects = json.loads(_run("pw-dump") or "[]")
    except ValueError:
        return []

    def props(obj):
        return (obj.get("info") or {}).get("props") or {}

    # The process lives on the Client object; its Node only points back by client.id.
    wine_clients = {obj["id"] for obj in objects
                    if obj.get("type", "").endswith("Client")
                    and props(obj).get("client.api") == "jack"
                    and WINE_BINARY_RE.match(str(props(obj).get("application.process.binary", "")))}
    errors = xruns() if wine_clients else {}
    found = []
    for obj in objects:
        node = props(obj)
        if not obj.get("type", "").endswith("Node") or node.get("client.id") not in wine_clients:
            continue
        match = re.match(r"(\d+)/(\d+)", str(node.get("node.latency", "")))
        found.append(Client(obj["id"], node.get("node.name") or node.get("client.name") or "?",
                            int(match.group(1)) if match else None,
                            int(match.group(2)) if match else None,
                            errors.get(obj["id"])))
    return found


def xruns():
    """Error (xrun) counters per node id from pw-top."""
    counts = {}
    for line in _run("pw-top", "-b", "-n", "2").splitlines():
        parts = line.split()
        if len(parts) > 8 and parts[1].isdigit() and parts[8].isdigit():
            counts[int(parts[1])] = int(parts[8])
    return counts


# ---------------------------------------------------------------------------
# System checks

def jack_library():
    """Path of the libjack.so.0 programs get, and whether it is PipeWire's."""
    for line in _run("ldconfig", "-p").splitlines():
        if "libjack.so.0 " in line and "x86-64" in line:
            path = line.split("=>")[-1].strip()
            return path, "pipewire" in path
    return None, False


def memlock_kib():
    """RLIMIT_MEMLOCK in KiB, or None for unlimited."""
    soft, _ = resource.getrlimit(resource.RLIMIT_MEMLOCK)
    return None if soft == resource.RLIM_INFINITY else soft // 1024
