"""DESCO প্রিপেইড মিটার প্রোভাইডার।

পোর্টাল: https://prepaid.desco.org.bd — সাধারণত Account No. দিয়ে ব্যালেন্স,
মাসিক ব্যবহার আর রিচার্জ হিস্ট্রি পাওয়া যায়।
"""

from __future__ import annotations

from .tkdes import TkdesProvider


class DescoProvider(TkdesProvider):
    name = "desco"
    label_bn = "DESCO"

    balance_paths = (
        "/api/prepaid/customer/balance",
        "/api/prepaid/balance",
        "/api/customer/balance",
        "/api/balance",
    )
    usage_paths = (
        "/api/prepaid/customer/consumption",
        "/api/prepaid/consumption",
        "/api/customer/usage",
    )
    recharge_paths = (
        "/api/prepaid/customer/recharge-history",
        "/api/prepaid/recharge",
    )

    @property
    def base_url(self) -> str:
        return self.settings.desco_base_url

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label_bn": self.label_bn,
            "supports_live": False,
            "base_url": self.base_url,
            "site": "prepaid.desco.org.bd",
            "account": self.account_no,
        }
