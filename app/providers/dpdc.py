"""DPDC প্রিপেইড মিটার প্রোভাইডার।

পোর্টাল: https://prepaid.dpdc.org.bd — Customer No. দিয়ে ব্যালেন্স/ব্যবহার।
"""

from __future__ import annotations

from .tkdes import TkdesProvider


class DpdcProvider(TkdesProvider):
    name = "dpdc"
    label_bn = "DPDC"

    balance_paths = (
        "/api/prepaid/customer-info",
        "/api/prepaid/customer/balance",
        "/api/customer/balance",
        "/api/prepaid/balance",
    )
    usage_paths = (
        "/api/prepaid/usage-history",
        "/api/prepaid/customer/consumption",
        "/api/customer/usage",
    )
    recharge_paths = (
        "/api/prepaid/recharge-history",
        "/api/prepaid/recharge",
    )

    @property
    def base_url(self) -> str:
        return self.settings.dpdc_base_url

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label_bn": self.label_bn,
            "supports_live": False,
            "base_url": self.base_url,
            "site": "prepaid.dpdc.org.bd",
            "account": self.account_no,
        }
