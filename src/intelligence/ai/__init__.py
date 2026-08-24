"""AI layer: local-first provider abstraction, context packets, structured proposals.

Boundaries (non-negotiable):
  * AI is OPTIONAL - everything else works with ai.enabled=false or the server down;
  * AI writes only to ai_proposals; acceptance goes through src/intelligence/ai/proposals.py
    which calls existing v2 services;
  * default provider is a LOCAL OpenAI-compatible server (llama.cpp); no cloud calls;
  * every persisted output records provider, model, prompt version and context hash;
  * the model is told to separate SOURCE FACT / CALCULATION / INTERPRETATION / HYPOTHESIS /
    UNKNOWN and to answer the contradiction-first question set.
"""
