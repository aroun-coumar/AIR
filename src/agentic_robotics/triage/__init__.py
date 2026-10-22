"""Triage: one queue for everything AIR finds wrong (ADR 0009).

    from agentic_robotics.triage import TriageBook, RuntimeTriageSink, sweep

    book = TriageBook(project)
    runtime.trace.add_sink(RuntimeTriageSink(book))     # live runtime outcomes
    report = sweep(book)                                # checks, work ledger, deadlines
    for item in book.items(overdue=True):
        print(item.severity, item.priority, item.title)
"""

from .book import IngestResult, TriageBook, TriageError, load_policy
from .collectors import RuntimeTriageSink, SweepReport, resource_listener, sweep
from .contracts import Priority, Severity, Signal, TriageItem, TriageState
from .policy import Inhibition, Matcher, Silence, TriagePolicy, TriageRule, default_policy

__all__ = [
    "IngestResult", "Inhibition", "Matcher", "Priority", "RuntimeTriageSink", "Severity",
    "Signal", "Silence", "SweepReport", "TriageBook", "TriageError", "TriageItem",
    "TriagePolicy", "TriageRule", "TriageState", "default_policy", "load_policy",
    "resource_listener", "sweep",
]
