"""Mentor & briefing layer (v4): DELTA, NOT STATE.

checkpoints  - persistent brief runs; the delta window is (last completed run, now]
deltas       - deterministic delta engine (Python decides what is new; never the LLM)
hygiene      - suppression rules (static risks/breakers do not reappear daily)
assemble     - deterministic daily/weekly brief construction + markdown/audio rendering
mentor       - single compact Glimmer synthesis call on top of the deterministic deltas
regime       - transparent macro regime dimensions with documented rules
calibration  - Brier/buckets when the resolved-prediction sample is large enough
"""
