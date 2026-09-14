"""SEC EDGAR submissions adapter with identifiable User-Agent and 2 req/s cap."""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from src.premarket.providers.artifacts import RawEvidenceStore
from src.premarket.providers.collector_types import CollectionContext
from src.premarket.providers.common import payload_sha256, result, unavailable
from src.premarket.providers.config import SecEdgarConfig
from src.premarket.providers.http import BoundedHttpClient, HttpCollectionError


def _iso_utc(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if text.endswith("Z"):
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    elif len(text) == 14 and text.isdigit():
        parsed = datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    else:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SecEdgarProvider:
    provider = "sec-edgar"
    kind = "FILING"

    def __init__(
        self,
        config: SecEdgarConfig,
        http: BoundedHttpClient,
        *,
        user_agent: str,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.http = http
        self.user_agent = user_agent.strip()
        self.sleep = sleep

    def collect(self, context: CollectionContext, raw_store: RawEvidenceStore) -> dict[str, Any]:
        if not self.config.enabled:
            return unavailable(self.provider, self.kind, self.config.base_url, context.as_of, "SEC_EDGAR_DISABLED")
        if not self.user_agent or "@" not in self.user_agent:
            return unavailable(self.provider, self.kind, self.config.base_url, context.as_of, "SEC_USER_AGENT_MISSING_OR_UNIDENTIFIABLE")
        targets = [(symbol, self.config.symbol_ciks[symbol]) for symbol in context.symbols if symbol in self.config.symbol_ciks]
        unmapped = sorted(set(context.symbols) - set(self.config.symbol_ciks))
        receipts: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        interval = 1.0 / self.config.requests_per_second
        for index, (symbol, cik) in enumerate(targets):
            if index:
                self.sleep(interval)
            url = f"{self.config.base_url}/CIK{cik}.json"
            try:
                payload, receipt = self.http.get_json(
                    url,
                    params=None,
                    headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
                    provider=self.provider,
                    resource=f"submissions-{symbol}",
                    raw_store=raw_store,
                )
                receipts.append(receipt)
            except HttpCollectionError as error:
                if error.receipt:
                    receipts.append(error.receipt)
                errors.append(f"{symbol}:{error.code}")
                continue
            filings = payload.get("filings")
            if not isinstance(filings, dict):
                errors.append(f"{symbol}:SEC_FILINGS_NOT_OBJECT")
                continue
            recent = filings.get("recent", {})
            if not isinstance(recent, dict):
                errors.append(f"{symbol}:SEC_RECENT_NOT_OBJECT")
                continue
            fields = ("accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form", "primaryDocument")
            arrays = [recent.get(field, []) for field in fields]
            if any(not isinstance(values, list) for values in arrays):
                errors.append(f"{symbol}:SEC_RECENT_FIELD_NOT_LIST")
                continue
            if len({len(values) for values in arrays}) != 1:
                errors.append(f"{symbol}:SEC_RECENT_FIELD_LENGTH_MISMATCH")
                continue
            for accession, filing_date, report_date, accepted, form, primary_document in zip(*arrays):
                form_text = str(form).upper()
                if form_text not in self.config.forms:
                    continue
                try:
                    accepted_at = _iso_utc(str(accepted))
                except ValueError:
                    errors.append(f"{symbol}:SEC_ACCEPTANCE_TIME_INVALID")
                    continue
                if not accepted_at:
                    errors.append(f"{symbol}:SEC_ACCEPTANCE_TIME_MISSING")
                    continue
                if accepted_at and accepted_at > context.as_of:
                    continue
                accession_text = str(accession)
                document = str(primary_document)
                accession_compact = accession_text.replace("-", "")
                filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_compact}/{document}"
                safe = {
                    "item_type": "FILING",
                    "item_id": f"sec:{accession_text}",
                    "symbols": [symbol],
                    "cik": cik,
                    "form": form_text,
                    "filing_date": str(filing_date),
                    "report_date": str(report_date),
                    "accepted_at": accepted_at,
                    "source_url": filing_url,
                }
                safe["content_sha256"] = payload_sha256(safe)
                items.append(safe)
        status = "ERROR" if errors else "OK"
        error_code = ";".join(sorted(errors)) or None
        output = result(
            self.provider,
            self.kind,
            status,
            self.config.base_url,
            context.as_of,
            items,
            receipts,
            error_code,
        )
        output["unmapped_symbols"] = unmapped
        return output
