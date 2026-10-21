"""প্রোভাইডার ফ্যাক্টরি।"""

from __future__ import annotations

from ..config import Settings
from ..tariff import Tariff
from .base import BaseProvider, ProviderError
from .demo import DemoProvider
from .desco import DescoProvider
from .dpdc import DpdcProvider

__all__ = [
    "BaseProvider",
    "DemoProvider",
    "DescoProvider",
    "DpdcProvider",
    "ProviderError",
    "build_provider",
]


def build_provider(settings: Settings, tariff: Tariff) -> BaseProvider:
    name = (settings.provider or "demo").lower()
    if name == "desco":
        return DescoProvider(settings)
    if name == "dpdc":
        return DpdcProvider(settings)
    if name == "demo":
        return DemoProvider(settings, tariff)
    raise ProviderError(
        f"অজানা প্রোভাইডার: {settings.provider}",
        kind="config",
        hint="PROVIDER=desco | dpdc | demo এর মধ্যে একটা দিন।",
    )
