"""Alerting.

    rules.py     the detectors — arithmetic only, no model, and mostly about
                 refusing to fire
    channels.py  dashboard and email delivery
    service.py   dedupe, cooldown, resolution and severity gating

The hard problem here is alert fatigue, not detection. See `service.py`.
"""
