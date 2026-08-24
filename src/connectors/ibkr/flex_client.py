"""IBKR Flex Web Service client - READ ONLY.

Flow: SendRequest(token, queryId) -> ReferenceCode -> GetStatement(token, referenceCode) -> XML.
This module never logs the token and never stores it in raw archives (the raw archive is the
statement body only, which does not contain the token).
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

from src.config import mask_secret
from src.logging_setup import get_logger

log = get_logger("ibkr.flex_client")

# Error codes where the statement is simply not ready yet (retry).
_RETRYABLE_CODES = {"1019", "1018", "1021"}


class FlexError(RuntimeError):
    pass


@dataclass
class FlexClient:
    token: str
    query_id: str
    base_url: str = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
    version: int = 3
    poll_interval_seconds: float = 5.0
    max_poll_attempts: int = 12
    timeout_seconds: float = 60.0
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        if not self.token or not self.query_id:
            raise FlexError("IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID must be set in .env")
        self._http = self.session or requests.Session()
        self._http.headers.update({"User-Agent": "InvestorOS/1.0 (read-only Flex client)"})

    # --- step 1 -------------------------------------------------------------
    def send_request(self) -> str:
        log.info("Flex SendRequest queryId=%s token=%s", self.query_id, mask_secret(self.token))
        resp = self._http.get(
            f"{self.base_url}/SendRequest",
            params={"t": self.token, "q": self.query_id, "v": str(self.version)},
            timeout=self.timeout_seconds,
        )
        resp.raise_for_status()
        status, code, message, reference = _parse_status_response(resp.text)
        if status != "Success" or not reference:
            raise FlexError(f"Flex SendRequest failed: code={code} message={message}")
        log.info("Flex SendRequest ok, reference code received")
        return reference

    # --- step 2 -------------------------------------------------------------
    def get_statement(self, reference_code: str) -> str:
        for attempt in range(1, self.max_poll_attempts + 1):
            resp = self._http.get(
                f"{self.base_url}/GetStatement",
                params={"t": self.token, "q": reference_code, "v": str(self.version)},
                timeout=self.timeout_seconds,
            )
            resp.raise_for_status()
            body = resp.text
            if _looks_like_statement(body):
                log.info("Flex GetStatement ok (%d bytes) after %d attempt(s)", len(body), attempt)
                return body
            status, code, message, _ = _parse_status_response(body)
            if code in _RETRYABLE_CODES:
                log.info("Flex statement not ready (code=%s), retry %d/%d", code, attempt, self.max_poll_attempts)
                time.sleep(self.poll_interval_seconds)
                continue
            raise FlexError(f"Flex GetStatement failed: status={status} code={code} message={message}")
        raise FlexError("Flex statement was not ready after maximum polling attempts")

    def fetch(self) -> str:
        """Full round-trip; returns the statement XML text."""
        return self.get_statement(self.send_request())


def _looks_like_statement(body: str) -> bool:
    head = body.lstrip()[:200]
    return "<FlexQueryResponse" in head


def _parse_status_response(body: str) -> tuple[str | None, str | None, str | None, str | None]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None, None, body[:200], None
    status = (root.findtext("Status") or "").strip() or None
    code = (root.findtext("ErrorCode") or "").strip() or None
    message = (root.findtext("ErrorMessage") or "").strip() or None
    reference = (root.findtext("ReferenceCode") or "").strip() or None
    return status, code, message, reference
