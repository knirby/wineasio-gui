"""wineasio-gui's own settings: prefixes added by hand and the last one used."""

import json
import os
from pathlib import Path

FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wineasio-gui" / "config.json"


def load():
    try:
        data = json.loads(FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temporary, FILE)


def added_prefixes():
    return [Path(path) for path in load().get("prefixes", [])]


def add_prefix(path):
    data = load()
    paths = data.setdefault("prefixes", [])
    if str(path) not in paths:
        paths.append(str(path))
    save(data)


def remove_prefix(path):
    data = load()
    data["prefixes"] = [p for p in data.get("prefixes", []) if p != str(path)]
    save(data)


def last_prefix():
    value = load().get("last_prefix")
    return Path(value) if value else None


def set_last_prefix(path):
    data = load()
    data["last_prefix"] = str(path)
    save(data)
