"""The agent.

Reading order, if you want to understand what this can and cannot do:

    policy.py        the allowlist and autonomy tiers — the security boundary
    evidence.py      the facts the model is given, and nothing else
    prompts.py       how content is fenced so it cannot become instruction
    grounding.py     how a fabricated figure is caught after the fact
    loop.py          the eight-stage cycle
    executor.py      the only path from a proposal to an effect
    verification.py  how past advice is graded against what happened
"""
