"""Finds the official fleet-kit in this repo and makes `fleetkit` importable.

Set FLEETKIT_DIR to override (the folder that contains `fleetkit/`).
"""
import os
import sys
from pathlib import Path


def find_kit() -> Path:
    env = os.environ.get("FLEETKIT_DIR")
    if env:
        return Path(env).resolve()
    root = Path(__file__).resolve().parent.parent
    for init in sorted(root.rglob("fleetkit/__init__.py")):
        if ".git" not in init.parts:
            return init.parent.parent
    raise SystemExit("fleet-kit not found; set FLEETKIT_DIR to the folder that contains fleetkit/")


KIT = find_kit()
sys.path.insert(0, str(KIT))
