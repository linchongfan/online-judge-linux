"""Load .env into os.environ without overriding existing variables."""

import os
from pathlib import Path


def load_env(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # Accept shell-style `export KEY=value` so the same file can be
        # sourced by bash and loaded here.
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value[:1] in ('"', "'"):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            # Unquoted values may carry inline comments (systemd semantics).
            value = value.split(" #", 1)[0].split("\t#", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value
