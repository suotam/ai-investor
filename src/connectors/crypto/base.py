"""Crypto connector interface.

v1 ships only CsvCryptoImporter. Future connectors (Anycoin API, exchange API, public
wallet) implement the same interface and feed the same idempotent importer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from src.core import ParsedStatement


class CryptoConnector(ABC):
    """Produces a ParsedStatement (normalized transactions + cash flows) for one account."""

    source: str = "crypto"

    @abstractmethod
    def load(self) -> ParsedStatement:  # pragma: no cover - interface
        raise NotImplementedError
