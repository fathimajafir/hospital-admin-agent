"""Minimum-necessary PHI masking.

Two entry points:

* ``redact_record``  - masks a dict for a caller that may not see PHI, or did not
  state a purpose of use.
* ``redact_text``    - a safety net for free text (logs, error messages, tool
  observations) that might carry identifiers.

The masking is intentionally lossy: it keeps enough shape for an operator to
recognise a record ("T. P***") without exposing identity.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

PHI_FIELDS: tuple[str, ...] = (
    "name",
    "dob",
    "phone",
    "email",
    "mrn",
    "address",
    "contact_name",
    "insurance_id",
)

_MRN_RE = re.compile(r"\bMRN[-: ]?([A-Z0-9]{4,12})\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
_PHONE_RE = re.compile(r"(?<![\w.])(\+?\d[\d ()-]{7,}\d)(?![\w.])")


def _mask_name(value: str) -> str:
    parts = [p for p in re.split(r"\s+", value.strip()) if p and not p[0].isdigit()]
    if not parts:
        return "***"
    return " ".join(f"{p[0].upper()}***" for p in parts)


def _mask_dob(value: str) -> str:
    # Keep the birth year only: useful for cohorting, not identifying.
    match = re.match(r"(\d{4})-", value)
    return f"{match.group(1)}-**-**" if match else "****-**-**"


def _mask_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return f"***-***-{digits[-4:]}" if len(digits) >= 4 else "***-***-****"


def _mask_email(value: str) -> str:
    _, _, domain = value.partition("@")
    return f"***@{domain}" if domain else "***"


def _mask_mrn(value: str) -> str:
    return f"***{value[-3:]}" if len(value) > 3 else "***"


def _mask_generic(value: str) -> str:
    return "***"


_MASKERS = {
    "name": _mask_name,
    "contact_name": _mask_name,
    "dob": _mask_dob,
    "phone": _mask_phone,
    "email": _mask_email,
    "mrn": _mask_mrn,
    "insurance_id": _mask_mrn,
    "address": _mask_generic,
}


def redact_record(
    record: dict[str, Any],
    *,
    allow_phi: bool,
    fields: Iterable[str] = PHI_FIELDS,
) -> tuple[dict[str, Any], list[str]]:
    """Return ``(safe_record, masked_field_names)``.

    Non-PHI keys are copied untouched, including nested dicts and lists.
    """
    if allow_phi:
        return record, []

    masked: list[str] = []
    out: dict[str, Any] = {}
    targets = set(fields)

    for key, value in record.items():
        if key in targets and isinstance(value, str) and value:
            out[key] = _MASKERS.get(key, _mask_generic)(value)
            masked.append(key)
        elif isinstance(value, dict):
            nested, nested_masked = redact_record(value, allow_phi=False, fields=fields)
            out[key] = nested
            masked.extend(f"{key}.{m}" for m in nested_masked)
        elif isinstance(value, list):
            items = []
            for item in value:
                if isinstance(item, dict):
                    nested, nested_masked = redact_record(item, allow_phi=False, fields=fields)
                    items.append(nested)
                    masked.extend(nested_masked)
                else:
                    items.append(item)
            out[key] = items
        else:
            out[key] = value

    return out, sorted(set(masked))


def redact_text(text: str) -> str:
    """Best-effort scrub of identifiers in free text."""
    if not text:
        return text
    text = _MRN_RE.sub(lambda m: f"MRN-**{m.group(1)[-3:]}", text)
    text = _EMAIL_RE.sub(lambda m: _mask_email(m.group(0)), text)
    text = _PHONE_RE.sub(lambda m: _mask_phone(m.group(0)), text)
    return text
