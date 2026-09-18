"""Strands Evals harness for medical nudging.

These checks answer "did the run work, and is the output ready for a clinician
to inspect?", not "was the advice clinically correct?".

This package owns the layered evaluation workflow; the legacy judges are retired.
"""

from evals.gates import CitationPresent, ExecutionHealth, OutputFormatSuccess, ReviewerReady

__all__ = ["OutputFormatSuccess", "CitationPresent", "ExecutionHealth", "ReviewerReady"]
