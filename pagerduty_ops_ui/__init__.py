"""The team web UI service (ADRs 0001–0015): a thin layer over the pagerduty_ops CLI engine.

It holds no command logic or safety rules of its own: every Run is the command's own
console script in its own process (ADR 0004), and what a Run may do comes from the
engine's risk declarations (ADR 0010).
"""
