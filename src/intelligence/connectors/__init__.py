"""External data connectors (SEC/EDGAR, FRED macro, insiders, 13F, congress files, news).

Each connector: downloads (rate-limited, retried) -> archives raw -> registers provenance ->
normalizes -> idempotent insert -> records intelligence events. All offline-testable via an
injectable HTTP fetch function.
"""
