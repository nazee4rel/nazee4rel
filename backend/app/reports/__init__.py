"""Daily, weekly and monthly reports.

builder.py   assembles structured sections from the analytics engine,
             the agent's stored insights and the alert history
service.py   persists a report, then delivers it — in that order, so a
             failing mail server never costs you the report
"""
