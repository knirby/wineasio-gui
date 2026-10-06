"""Wine prefixes, the WineASIO driver, and WineASIO's per-prefix settings."""

import glob
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, fields
from pathlib import Path

CLSID = "{48D0C522-BFCC-45CC-8B84-17F25F33E6E8}"
KEY = "Software\\Wine\\WineASIO"
BUFFER_MIN, BUFFER_MAX = 16, 8192

# Settings attribute -> (registry value, environment variable that overrides it)
VALUES = {
    "inputs": ("Number of inputs", "WINEASIO_NUMBER_INPUTS"),
    "outputs": ("Number of outputs", "WINEASIO_NUMBER_OUTPUTS"),
    "connect_to_hardware": ("Connect to hardware", "WINEASIO_CONNECT_TO_HARDWARE"),
    "fixed_buffersize": ("Fixed buffersize", "WINEASIO_FIXED_BUFFERSIZE"),
    "preferred_buffersize": ("Preferred buffersize", "WINEASIO_PREFERRED_BUFFERSIZE"),
    "autostart_server": ("Autostart server", "WINEASIO_AUTOSTART_SERVER"),
}


@dataclass
class Settings:
    """WineASIO's settings, with the driver's own defaults."""
    inputs: int = 16
    outputs: int = 16
    connect_to_hardware: bool = True
    fixed_buffersize: bool = True
    preferred_buffersize: int = 1024
    autostart_server: bool = False

    @classmethod
    def from_values(cls, values):
        settings = cls()
        for attribute, (name, _) in VALUES.items():
            if name.lower() in values:
                kind = type(getattr(settings, attribute))
                setattr(settings, attribute, kind(values[name.lower()]))
        return settings

    def to_values(self):
        return {VALUES[f.name][0]: int(getattr(self, f.name)) for f in fields(self)}


def environment_overrides(environ=None):
    """WINEASIO_* variables set here; WineASIO prefers them over the registry."""
    environ = os.environ if environ is None else environ
    return {name: environ[name] for _, name in VALUES.values() if name in environ}


# ---------------------------------------------------------------------------
# Wine's registry files

def _unescape(text):
    return re.sub(r"\\(.)", r"\1", text)


def _escape_key(key):
    return key.replace("\\", "\\\\")


def read_key(reg_file, key):
    """DWORD values of one key in a Wine .reg file, by lowercase value name."""
    values, inside = {}, False
    try:
        lines = Path(reg_file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        if line.startswith("["):
            name = line[1:line.rfind("]")]
            inside = _unescape(name).lower() == key.lower()
        elif inside:
            match = re.match(r'^"((?:[^"\\]|\\.)*)"=dword:([0-9a-fA-F]{1,8})$', line)
            if match:
                values[_unescape(match.group(1)).lower()] = int(match.group(2), 16)
    return values


def write_key(reg_file, key, values):
    """Set DWORD values of one key in a Wine .reg file, keeping everything else.

    Only safe while no wineserver runs for the prefix; a running server rewrites
    the file from memory.
    """
    path = Path(reg_file)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    rendered = {name: f'"{name}"=dword:{value:08x}' for name, value in values.items()}
    start = next((index for index, line in enumerate(lines) if line.startswith("[")
                  and _unescape(line[1:line.rfind("]")]).lower() == key.lower()), None)
    if start is None:
        now = int(time.time())
        filetime = (now + 11644473600) * 10_000_000
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{_escape_key(key)}] {now}", f"#time={filetime:x}"]
        lines += [rendered[name] for name in sorted(rendered)]
        lines.append("")
    else:
        end = start + 1
        while end < len(lines) and lines[end].strip() and not lines[end].startswith("["):
            end += 1
        pending = dict(rendered)
        for index in range(start + 1, end):
            match = re.match(r'^"((?:[^"\\]|\\.)*)"=', lines[index])
            if match:
                for name in list(pending):
                    if _unescape(match.group(1)).lower() == name.lower():
                        lines[index] = pending.pop(name)
        lines[end:end] = [pending[name] for name in sorted(pending)]
    backup = path.with_name(path.name + ".bak-wineasio-gui")
    if not backup.exists():
        shutil.copy2(path, backup)
    temporary = path.with_name(f".{path.name}.wineasio-gui")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# Prefixes

@dataclass
class Prefix:
    path: Path
    label: str

    def is_prefix(self):
        return all((self.path / name).exists() for name in ("system.reg", "user.reg", "drive_c"))

    @property
    def registered(self):
        """True when WineASIO's COM class is registered in this prefix."""
        try:
            with open(self.path / "system.reg", encoding="utf-8", errors="replace") as handle:
                return any(CLSID.lower() in line.lower() for line in handle)
        except OSError:
            return False

    def settings(self):
        return Settings.from_values(read_key(self.path / "user.reg", KEY))

    def server_dir(self):
        stat = os.stat(self.path)
        return Path(f"/tmp/.wine-{os.getuid()}/server-{stat.st_dev:x}-{stat.st_ino:x}")

    def wineserver(self):
        """Executable of the wineserver running this prefix, or None."""
        try:
            target = str(self.server_dir())
        except OSError:
            return None
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                if (proc / "comm").read_text().startswith("wineserver") \
                        and os.readlink(proc / "cwd") == target:
                    return Path(os.readlink(proc / "exe"))
            except OSError:
                continue
        return None

    def wine(self, preferred=None):
        """The wine to run in this prefix: the running server's own, else preferred."""
        server = self.wineserver()
        if server:
            for name in (server.name.replace("wineserver", "wine"), "wine", "wine64"):
                candidate = server.parent / name
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
        return preferred or shutil.which("wine")

    def save(self, settings, wine=None):
        """Store settings; through the running wineserver if there is one."""
        values = settings.to_values()
        if self.wineserver():
            lines = ["Windows Registry Editor Version 5.00", "", f"[HKEY_CURRENT_USER\\{KEY}]"]
            lines += [f'"{name}"=dword:{value:08x}' for name, value in values.items()]
            with tempfile.NamedTemporaryFile("w", suffix=".reg", delete=False) as handle:
                handle.write("\r\n".join(lines) + "\r\n")
            try:
                run_wine(self, ["regedit", "/S", handle.name], wine)
            finally:
                os.unlink(handle.name)
        else:
            write_key(self.path / "user.reg", KEY, values)

    def register(self, wine=None):
        """Register WineASIO's COM class here (both architectures that are installed)."""
        done = []
        for dll in ("wineasio64.dll", "wineasio32.dll", "wineasio.dll"):
            result = run_wine(self, ["regsvr32", "/s", dll], wine, check=False)
            if result.returncode == 0:
                done.append(dll)
        return done


def run_wine(prefix, arguments, wine=None, check=True):
    executable = prefix.wine(wine)
    if not executable:
        raise FileNotFoundError("wine was not found")
    environment = dict(os.environ, WINEPREFIX=str(prefix.path), WINEDEBUG="-all")
    result = subprocess.run([executable, *arguments], env=environment, capture_output=True,
                            text=True, timeout=180)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or f"{arguments[0]} failed")
    return result


def _label(path, home):
    for marker, name in (("/bottles/bottles/", "Bottles"),):
        if marker in str(path):
            return f"{name}: {path.name}"
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def discover(extra=(), home=None):
    """Wine prefixes in the usual places, plus any added by hand."""
    home = Path(home or Path.home())
    patterns = [
        ".wine", ".wine*", ".local/share/wineprefixes/*", "Wine/*", "Wine/*/*",
        "Games/*", ".local/share/bottles/bottles/*",
        ".var/app/com.usebottles.bottles/data/bottles/bottles/*",
    ]
    found, seen = [], set()
    candidates = [Path(p) for pattern in patterns for p in sorted(glob.glob(str(home / pattern)))]
    for path in [*map(Path, extra), *candidates]:
        path = path.expanduser()
        try:
            real = path.resolve()
        except OSError:
            continue
        if real in seen:
            continue
        prefix = Prefix(path, _label(path, home))
        if prefix.is_prefix():
            seen.add(real)
            found.append(prefix)
    return found


# ---------------------------------------------------------------------------
# The driver itself

def installed_drivers():
    """Wine library directories that hold WineASIO's Unix half: (directory, arch)."""
    home = Path.home()
    roots = ["/usr/lib", "/usr/lib64", "/usr/lib32", "/usr/lib/x86_64-linux-gnu",
             "/usr/lib/i386-linux-gnu", "/usr/local/lib", "/usr/local/lib64"]
    patterns = [f"{root}/wine*/{{arch}}-unix" for root in roots]
    patterns += [f"{root}/wine*/wine/{{arch}}-unix" for root in roots]
    patterns += ["/opt/wine*/lib*/wine/{arch}-unix",
                 str(home / ".local/share/bottles/runners/*/lib*/wine/{arch}-unix"),
                 str(home / ".local/share/lutris/runners/wine/*/lib*/wine/{arch}-unix")]
    found = []
    for arch in ("x86_64", "i386"):
        for pattern in patterns:
            for directory in sorted(glob.glob(pattern.format(arch=arch))):
                if glob.glob(f"{directory}/wineasio*.so"):
                    found.append((directory, arch))
    return found
