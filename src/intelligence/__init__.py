"""Investor OS v3 intelligence layer.

Additive on top of v1 (portfolio) and v2 (research). Pipeline:

    DATA -> NORMALIZED FACT -> EVIDENCE / KPI OBSERVATION -> INTERPRETATION
         -> AI PROPOSAL -> HUMAN REVIEW -> ACCEPT/REJECT -> THESIS REVISION (v2 service)

Hard rules enforced here:
  * every external item has provenance (source_documents + local raw archive),
  * ingestion is idempotent (external ids + content hashes),
  * AI writes ONLY to ai_proposals; accepted proposals call existing v2 services,
  * the whole layer is optional - portfolio and research work without it and without AI.
"""
