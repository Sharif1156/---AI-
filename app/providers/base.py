"""প্রোভাইডার ইন্টারফেস — নতুন সোর্স (ESP32 সেন্সর, অন্য ইউটিলিটি) যোগ করা সহজ।"""

from __future__ import annotations

import abc

from ..models import Snapshot


class ProviderError(RuntimeError):
    """প্রোভাইডার থেকে ডেটা আনতে সমস্যা হলে ছোড়া হয়।"""

    def __init__(self, message: str, *, kind: str = "upstream", hint: str = ""):
        super().__init__(message)
        self.kind = kind  # upstream | auth | network | parse | config
        self.hint = hint

    def to_dict(self) -> dict[str, str]:
        return {"message": str(self), "kind": self.kind, "hint": self.hint}


class BaseProvider(abc.ABC):
    name: str = "base"
    label_bn: str = "আধার"
    supports_live: bool = False

    @abc.abstractmethod
    async def fetch(self) -> Snapshot:
        """সর্বশেষ মিটার স্ন্যাপশট ফেরত দেয়।"""

    async def aclose(self) -> None:  # pragma: no cover - ঐচ্ছিক ক্লিনআপ
        return None

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label_bn": self.label_bn,
            "supports_live": self.supports_live,
        }
