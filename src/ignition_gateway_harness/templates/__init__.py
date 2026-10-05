"""Simulation and peripheral template assets for Ignition Gateway Harness."""

from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent


def get_template_path(name: str) -> Path:
    """Return absolute path to a template asset file."""
    return TEMPLATES_DIR / name


def read_template_text(name: str) -> str:
    """Read and return content of a template file as string."""
    path = get_template_path(name)
    return path.read_text(encoding="utf-8")
