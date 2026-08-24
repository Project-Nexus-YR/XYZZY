"""Multiplayer AI Workspace - persistent shared workspace for humans and agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("multiplayer-ai")
except PackageNotFoundError:
    # Running from a checkout that was never installed.
    __version__ = "0.0.0+uninstalled"
