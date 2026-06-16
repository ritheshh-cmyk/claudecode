"""Wafer provider exports."""

from providers.defaults import WAFER_DEFAULT_BASE

from .client import WaferProvider

__all__ = [
    "WAFER_DEFAULT_BASE",
    "WaferProvider",
]
