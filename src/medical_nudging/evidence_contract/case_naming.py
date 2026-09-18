"""Strands Evals case-name namespace for the perturbation benchmark (design doc §8).

``LocalFileTaskResultStore`` keys purely on ``case.name``, so the name *is* the
namespace.  Every field that distinguishes one run from another has to survive inside
it::

    {benchmark_version}__{split}__{control}__{patient_short}__{nudge_idx}__{evaluator_id}
    pbench-v1__dev__patient_swap__0c2243__1__ece-v1

Result-store paths mirror the same fields (see :func:`result_store_subpath`).

The double underscore is the field separator, so no field may contain one.  Tokens are
therefore restricted to alphanumerics joined by single ``_`` or ``-`` separators, which
keeps :func:`build_case_name` and :func:`parse_case_name` exact inverses.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

FIELD_SEPARATOR = "__"

ORIGINAL_CONTROL_LABEL = "original"
"""Control token for a benchmark original.

The design doc leaves the control block ``null`` for the original case but still
requires a ``{control}`` field in the case name; ``original`` fills that slot.
"""

PATIENT_SHORT_LENGTH = 6

_TOKEN_RE = re.compile(r"^[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*$")


class CaseNameError(ValueError):
    """Raised when a case name or one of its fields is not round-trippable."""


@dataclass(frozen=True)
class CaseName:
    """The parsed fields of a benchmark case name."""

    benchmark_version: str
    split: str
    control: str
    patient_short: str
    nudge_idx: int
    evaluator_id: str

    def render(self) -> str:
        """Render back to the flat ``case.name`` string."""
        return build_case_name(
            benchmark_version=self.benchmark_version,
            split=self.split,
            control=self.control,
            patient_short=self.patient_short,
            nudge_idx=self.nudge_idx,
            evaluator_id=self.evaluator_id,
        )


def patient_short(patient_id: str, length: int = PATIENT_SHORT_LENGTH) -> str:
    """Shorten a patient identifier to a stable, separator-safe token.

    A hyphenated FHIR/UUID-style id contributes its own leading hex characters so the
    token stays recognisable against the source artifacts; anything else is hashed.
    Nothing here is reversible, and the token is not a de-identification claim — it
    only has to be short and stable.
    """
    if length <= 0:
        raise CaseNameError(f"patient_short length must be positive, got {length}")

    condensed = re.sub(r"[^A-Za-z0-9]", "", patient_id)
    if len(condensed) >= length and re.fullmatch(r"[0-9a-fA-F]+", condensed[:length]):
        return condensed[:length].lower()
    digest = hashlib.sha256(patient_id.encode("utf-8")).hexdigest()
    return digest[:length]


def _validate_token(field: str, value: str) -> str:
    if not _TOKEN_RE.match(value):
        raise CaseNameError(
            f"{field}={value!r} is not a valid case-name token: expected alphanumerics "
            "joined by single '_' or '-' (no '__', no leading/trailing separator)"
        )
    return value


def build_case_name(
    *,
    benchmark_version: str,
    split: str,
    control: str,
    patient_short: str,
    nudge_idx: int,
    evaluator_id: str,
) -> str:
    """Build a ``case.name`` from its fields.

    Args:
        benchmark_version: e.g. ``pbench-v1``.
        split: ``dev`` or ``test`` (design doc §5; validated by the schema, not here).
        control: a control type, or :data:`ORIGINAL_CONTROL_LABEL` for the original.
        patient_short: shortened patient token; see :func:`patient_short`.
        nudge_idx: index of the nudge within its source generation run.
        evaluator_id: condition plus version, e.g. ``judge-sol-v1`` or ``ece-v1``.

    Raises:
        CaseNameError: when any field would not survive a round trip.
    """
    if nudge_idx < 0:
        raise CaseNameError(f"nudge_idx must be non-negative, got {nudge_idx}")
    fields = [
        _validate_token("benchmark_version", benchmark_version),
        _validate_token("split", split),
        _validate_token("control", control),
        _validate_token("patient_short", patient_short),
        str(nudge_idx),
        _validate_token("evaluator_id", evaluator_id),
    ]
    return FIELD_SEPARATOR.join(fields)


def parse_case_name(case_name: str) -> CaseName:
    """Parse a ``case.name`` back into its fields.

    Raises:
        CaseNameError: when the name does not have exactly six well-formed fields.
    """
    fields = case_name.split(FIELD_SEPARATOR)
    if len(fields) != 6:
        raise CaseNameError(
            f"case name {case_name!r} has {len(fields)} fields, expected 6 separated by "
            f"{FIELD_SEPARATOR!r}"
        )
    benchmark_version, split, control, short, nudge_idx, evaluator_id = fields
    for name, value in (
        ("benchmark_version", benchmark_version),
        ("split", split),
        ("control", control),
        ("patient_short", short),
        ("evaluator_id", evaluator_id),
    ):
        _validate_token(name, value)
    if not nudge_idx.isdigit():
        raise CaseNameError(f"nudge_idx={nudge_idx!r} is not a non-negative integer")
    return CaseName(
        benchmark_version=benchmark_version,
        split=split,
        control=control,
        patient_short=short,
        nudge_idx=int(nudge_idx),
        evaluator_id=evaluator_id,
    )


def result_store_subpath(
    *,
    benchmark_version: str,
    split: str,
    evaluator_id: str,
) -> PurePosixPath:
    """Result-store path mirroring the case-name fields (design doc §8).

    ``results/perturbation-benchmark/{benchmark_version}/{split}/{evaluator_id}/``
    """
    for name, value in (
        ("benchmark_version", benchmark_version),
        ("split", split),
        ("evaluator_id", evaluator_id),
    ):
        _validate_token(name, value)
    return (
        PurePosixPath("results/perturbation-benchmark") / benchmark_version / split / evaluator_id
    )
