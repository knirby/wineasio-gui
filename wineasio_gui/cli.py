"""Command line interface. Without a command, wineasio-gui opens the window."""

import argparse
import os
import sys
from pathlib import Path

from . import __version__, config, pipewire, wine

BOOLEAN_KEYS = {"connect", "fixed", "autostart"}
KEYS = {
    "inputs": "inputs", "outputs": "outputs", "connect": "connect_to_hardware",
    "fixed": "fixed_buffersize", "preferred": "preferred_buffersize",
    "autostart": "autostart_server",
}


def pick_prefix(argument):
    """--prefix, else $WINEPREFIX, else the last one used, else ~/.wine."""
    for candidate in (argument, os.environ.get("WINEPREFIX"), config.last_prefix(),
                      Path.home() / ".wine"):
        if candidate:
            prefix = wine.Prefix(Path(candidate).expanduser(), str(candidate))
            if prefix.is_prefix():
                return prefix
            if candidate == argument:
                sys.exit(f"{candidate} is not a Wine prefix")
    sys.exit("no Wine prefix found; pass --prefix")


def parse_bool(text):
    lowered = text.lower()
    if lowered in {"1", "on", "yes", "true"}:
        return True
    if lowered in {"0", "off", "no", "false"}:
        return False
    raise ValueError(f"expected on or off, got {text!r}")


def show_audio():
    rate = pipewire.graph_rate()
    size = pipewire.buffer_size()
    pinned = pipewire.sample_rate()
    print(f"Buffer size:  {f'{size} samples ({pipewire.latency_ms(size, rate):.1f} ms)' if size else 'PipeWire default'}")
    print(f"Sample rate:  {rate} Hz{'' if pinned else ' (system default)'}")


def command_status(_):
    show_audio()
    running = pipewire.clients()
    print("\nRunning ASIO apps:" if running else "\nNo ASIO apps running.")
    for client in running:
        xruns = "" if client.xruns is None else f", {client.xruns} xruns"
        print(f"  {client.name}: {client.buffer} samples at {client.rate} Hz{xruns}")


def command_buffer(args):
    if args.size is None:
        show_audio()
        return
    if args.size == "default":
        pipewire.set_buffer_size(None)
        print("Buffer size: PipeWire default.")
    else:
        size = int(args.size)
        if size not in pipewire.BUFFER_SIZES:
            sys.exit(f"buffer size must be one of {', '.join(map(str, pipewire.BUFFER_SIZES))}")
        pipewire.set_buffer_size(size)
        print(f"Buffer size: {size} samples ({pipewire.latency_ms(size):.1f} ms).")
    print("Apps pick this up the next time they open their audio device.")


def command_rate(args):
    if args.rate is None:
        show_audio()
        return
    if args.rate == "default":
        pipewire.set_sample_rate(None)
        print(f"Sample rate: system default ({pipewire.graph_rate()} Hz).")
    else:
        rate = int(args.rate)
        if rate not in pipewire.RATES:
            sys.exit(f"sample rate must be one of {', '.join(map(str, pipewire.RATES))}")
        pipewire.set_sample_rate(rate)
        print(f"Sample rate: {rate} Hz, now and after restarts.")
    print("Reopen the audio device in your app to use it.")


def command_prefixes(_):
    for prefix in wine.discover(config.added_prefixes()):
        state = "registered" if prefix.registered else "not registered"
        running = ", running" if prefix.wineserver() else ""
        print(f"{prefix.label}  ({state}{running})\n    {prefix.path}")


def command_show(args):
    prefix = pick_prefix(args.prefix)
    settings = prefix.settings()
    print(f"Prefix: {prefix.path}")
    print(f"  registered:  {'yes' if prefix.registered else 'no'}")
    for key, attribute in KEYS.items():
        value = getattr(settings, attribute)
        print(f"  {key + ':':12} {('on' if value else 'off') if key in BOOLEAN_KEYS else value}")
    for name, value in wine.environment_overrides().items():
        print(f"  note: {name}={value} is set and overrides the registry")


def command_set(args):
    prefix = pick_prefix(args.prefix)
    settings = prefix.settings()
    for assignment in args.values:
        key, _, text = assignment.partition("=")
        if key not in KEYS or not text:
            sys.exit(f"expected key=value with key one of {', '.join(KEYS)}")
        try:
            value = parse_bool(text) if key in BOOLEAN_KEYS else int(text)
        except ValueError as error:
            sys.exit(str(error))
        if key == "preferred" and not wine.BUFFER_MIN <= value <= wine.BUFFER_MAX:
            sys.exit(f"preferred must be between {wine.BUFFER_MIN} and {wine.BUFFER_MAX}")
        if key in {"inputs", "outputs"} and not 0 <= value <= 128:
            sys.exit(f"{key} must be between 0 and 128")
        setattr(settings, KEYS[key], value)
    prefix.save(settings)
    config.set_last_prefix(prefix.path)
    print(f"Saved for {prefix.path}. Apps use it the next time they open WineASIO.")


def command_register(args):
    prefix = pick_prefix(args.prefix)
    done = prefix.register()
    if not done:
        sys.exit("Registration failed: is WineASIO installed for this prefix's Wine? "
                 "Run `wineasio-gui doctor`.")
    print(f"Registered {', '.join(done)} in {prefix.path}.")


def command_add_prefix(args):
    path = Path(args.path).expanduser().resolve()
    if not wine.Prefix(path, "").is_prefix():
        sys.exit(f"{path} is not a Wine prefix")
    config.add_prefix(path)
    print(f"Added {path}.")


def command_doctor(_):
    drivers = wine.installed_drivers()
    print("WineASIO driver:")
    for directory, arch in drivers:
        print(f"  {arch}: {directory}")
    if not drivers:
        print("  not found in any Wine library directory")
    library, ours = pipewire.jack_library()
    print(f"JACK library: {library or 'missing'}{' (PipeWire)' if ours else ''}")
    if library and not ours:
        print("  note: this is not PipeWire's libjack; WineASIO will need a JACK server")
    limit = pipewire.memlock_kib()
    print(f"Memory lock limit: {'unlimited' if limit is None else f'{limit} KiB'}")
    if limit is not None and limit < 1024 * 1024:
        print("  note: stock WineASIO locks all memory when it starts and can hang with a\n"
              "  small limit; see patches/wineasio-memlock.patch or raise the limit")
    for name, value in wine.environment_overrides().items():
        print(f"Environment: {name}={value} overrides every prefix's registry value")
    show_audio()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="wineasio-gui",
        description="Configure WineASIO: buffer size, sample rate and per-prefix settings. "
                    "Run without a command to open the window.")
    parser.add_argument("--version", action="version", version=f"wineasio-gui {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("status", help="audio settings and running ASIO apps")
    sub = commands.add_parser("buffer", help="show or set the buffer size for all Wine apps")
    sub.add_argument("size", nargs="?", help=f"one of {', '.join(map(str, pipewire.BUFFER_SIZES))}, or default")
    sub = commands.add_parser("rate", help="show or set PipeWire's sample rate")
    sub.add_argument("rate", nargs="?", help=f"one of {', '.join(map(str, pipewire.RATES))}, or default")
    commands.add_parser("prefixes", help="list the Wine prefixes found")
    for name, text in (("show", "show a prefix's WineASIO settings"),
                       ("set", "change a prefix's WineASIO settings"),
                       ("register", "register WineASIO in a prefix")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("-p", "--prefix", help="Wine prefix (default: $WINEPREFIX or the last used)")
        if name == "set":
            sub.add_argument("values", nargs="+", metavar="key=value",
                             help=f"keys: {', '.join(KEYS)} (on/off for connect, fixed, autostart)")
    sub = commands.add_parser("add-prefix", help="remember a prefix in a non-standard place")
    sub.add_argument("path")
    commands.add_parser("doctor", help="check the driver, JACK library and memory lock limit")
    args = parser.parse_args(argv)
    if args.command is None:
        from .gui import run
        return run()
    handler = {"status": command_status, "buffer": command_buffer, "rate": command_rate,
               "prefixes": command_prefixes, "show": command_show, "set": command_set,
               "register": command_register, "add-prefix": command_add_prefix,
               "doctor": command_doctor}[args.command]
    return handler(args) or 0
