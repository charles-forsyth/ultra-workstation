"""Ultra AI Workstation Desktop."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ultra-workstation")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+unknown"
