"""Pull FHIR resources into the local store.

The job is idempotent: resource identity comes from the FHIR logical id, kept in
``fhir_links`` so a patient keyed by MRN still resolves. Running a sync twice
updates rows instead of duplicating them, and the second run reports zero
creates - which is the property a nightly cron actually depends on.

Order is not arbitrary. Patients first (a Coverage with no patient is useless),
then Coverage to patch the payer, then Appointments and Claims which need a
resolvable patient.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..audit import AuditLog
from ..models import Principal, Role, utcnow
from . import mapping
from .client import FhirClient
from .errors import FhirError
from .mapping import MappingError

INGEST_ORDER = ("Patient", "Coverage", "Appointment", "Claim")

MAX_WARNINGS = 200


@dataclass
class IngestReport:
    source: str
    dry_run: bool = False
    started_at: str = field(default_factory=utcnow)
    duration_ms: int = 0
    created: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    warnings_dropped: int = 0
    fetched: dict[str, int] = field(default_factory=dict)

    def bump(self, bucket: dict[str, int], resource_type: str) -> None:
        bucket[resource_type] = bucket.get(resource_type, 0) + 1

    def warn(self, resource_type: str, resource_id: str | None, message: str) -> None:
        if len(self.warnings) >= MAX_WARNINGS:
            self.warnings_dropped += 1
            return
        self.warnings.append({"resource_type": resource_type, "resource_id": resource_id, "message": message})

    @property
    def totals(self) -> dict[str, int]:
        return {
            "created": sum(self.created.values()),
            "updated": sum(self.updated.values()),
            "skipped": sum(self.skipped.values()),
            "warnings": len(self.warnings) + self.warnings_dropped,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "created": dict(sorted(self.created.items())),
            "updated": dict(sorted(self.updated.items())),
            "skipped": dict(sorted(self.skipped.items())),
            "totals": self.totals,
            "warnings_dropped": self.warnings_dropped,
            "fetched": dict(sorted(self.fetched.items())),
            "warnings": self.warnings,
        }


class FhirIngest:
    def __init__(
        self,
        store: Any,
        settings: Any,
        *,
        audit: AuditLog | None = None,
        principal: Principal | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.audit = audit
        self.principal = principal or Principal(
            sub="fhir-sync",
            role=Role.service,
            tenant="default",
            purposes=("operations",),
        )

    # -- entry points ------------------------------------------------------
    def sync(
        self,
        client: FhirClient,
        resource_types: Iterable[str] | None = None,
        *,
        dry_run: bool = False,
        source: str | None = None,
    ) -> IngestReport:
        label = source or client.source_label()
        report = IngestReport(source=label, dry_run=dry_run)
        started = time.perf_counter()

        requested = list(resource_types or INGEST_ORDER)
        unknown = [t for t in requested if t not in INGEST_ORDER]
        if unknown:
            raise ValueError(f"unsupported resource types: {', '.join(unknown)}")

        index = self._patient_index(label)
        try:
            for resource_type in INGEST_ORDER:
                if resource_type not in requested:
                    continue
                resources = client.search(resource_type)
                report.fetched[resource_type] = len(resources)
                for resource in resources:
                    self._apply(resource, label, report, index, dry_run=dry_run)
        except FhirError as exc:
            report.duration_ms = int((time.perf_counter() - started) * 1000)
            self._audit(report, outcome="error", error=str(exc)[:200])
            raise

        report.duration_ms = int((time.perf_counter() - started) * 1000)
        self._audit(report, outcome="ok")
        return report

    def apply_bundle(
        self,
        bundle: dict[str, Any],
        *,
        source: str = "bundle",
        dry_run: bool = False,
    ) -> IngestReport:
        report = IngestReport(source=source, dry_run=dry_run)
        started = time.perf_counter()

        entries = bundle.get("entry") if isinstance(bundle, dict) else None
        resources = [
            (entry or {}).get("resource")
            for entry in (entries or [])
            if isinstance((entry or {}).get("resource"), dict)
        ]
        if isinstance(bundle, dict) and bundle.get("resourceType") not in {"Bundle", None}:
            resources = [bundle]

        # A Bundle is not ordered for our benefit: a Claim may precede the Patient
        # it points at. Sort into the same dependency order the live sync uses.
        resources.sort(key=lambda r: _order_key(str(r.get("resourceType") or "")))

        index = self._patient_index(source)
        seen: set[tuple[str, str]] = set()
        for resource in resources:
            kind = str(resource.get("resourceType") or "")
            rid = str(resource.get("id") or "")
            if (kind, rid) in seen:
                continue
            seen.add((kind, rid))
            self._apply(resource, source, report, index, dry_run=dry_run)

        report.duration_ms = int((time.perf_counter() - started) * 1000)
        self._audit(report, outcome="ok")
        return report

    # -- per resource ------------------------------------------------------
    def _apply(
        self,
        resource: dict[str, Any],
        source: str,
        report: IngestReport,
        index: dict[str, str],
        *,
        dry_run: bool,
    ) -> None:
        kind = str(resource.get("resourceType") or "")
        rid = str(resource.get("id") or "") or None

        if kind not in INGEST_ORDER:
            report.warn(kind or "unknown", rid, "resource type is not ingested")
            report.bump(report.skipped, kind or "unknown")
            return

        try:
            if kind == "Patient":
                self._patient(resource, source, report, index, dry_run=dry_run)
            elif kind == "Coverage":
                self._coverage(resource, source, report, index, dry_run=dry_run)
            elif kind == "Appointment":
                self._appointment(resource, source, report, index, dry_run=dry_run)
            else:
                self._claim(resource, source, report, index, dry_run=dry_run)
        except MappingError as exc:
            report.warn(kind, rid, str(exc))
            report.bump(report.skipped, kind)

    def _patient(self, resource, source, report, index, *, dry_run):
        mapped = mapping.patient_row(resource, mrn_system=self.settings.fhir_mrn_system)
        for message in mapped.warnings:
            report.warn("Patient", mapped.external_id, message)

        exists = self.store.get_patient(mapped.row["id"]) is not None
        if not dry_run:
            outcome = self.store.upsert_patient(mapped.row)
            self.store.link_fhir(source, "Patient", mapped.external_id, mapped.row["id"])
        else:
            outcome = "updated" if exists else "created"
        report.bump(report.updated if outcome == "updated" else report.created, "Patient")
        index[mapped.external_id] = mapped.row["id"]

    def _coverage(self, resource, source, report, index, *, dry_run):
        logical_id, payer = mapping.coverage_payer(resource)
        patient_id = self._resolve_patient(logical_id, index)
        if patient_id is None:
            raise MappingError(f"patient {logical_id} is not in the store; sync Patients first")

        if dry_run:
            outcome = "updated"
        else:
            changed = self.store.patch_patient_payer(patient_id, payer)
            outcome = "updated" if changed else "skipped"
            self.store.link_fhir(source, "Coverage", str(resource["id"]), patient_id)

        if outcome == "skipped":
            report.bump(report.skipped, "Coverage")
        else:
            report.bump(report.updated, "Coverage")

    def _appointment(self, resource, source, report, index, *, dry_run):
        _, logical_id = mapping.reference_id(self._patient_reference(resource))
        patient_id = self._resolve_patient(logical_id, index)
        if patient_id is None:
            raise MappingError(f"patient {logical_id} is not in the store; sync Patients first")

        mapped = mapping.appointment_row(resource, patient_id=patient_id)
        for message in mapped.warnings:
            report.warn("Appointment", mapped.external_id, message)

        if dry_run:
            exists = self.store.get_appointment(mapped.row["id"]) is not None
            outcome = "updated" if exists else "created"
        else:
            outcome = self.store.upsert_appointment(mapped.row)
            self.store.link_fhir(source, "Appointment", mapped.external_id, mapped.row["id"])
        report.bump(report.updated if outcome == "updated" else report.created, "Appointment")

    def _claim(self, resource, source, report, index, *, dry_run):
        _, logical_id = mapping.reference_id(self._patient_reference(resource))
        patient_id = self._resolve_patient(logical_id, index)
        if patient_id is None:
            raise MappingError(f"patient {logical_id} is not in the store; sync Patients first")

        patient = self.store.get_patient(patient_id) or {}
        mapped = mapping.claim_row(resource, patient_id=patient_id, payer_fallback=patient.get("payer", ""))
        for message in mapped.warnings:
            report.warn("Claim", mapped.external_id, message)
        if not mapped.row["id"]:
            raise MappingError("Claim has no logical id")

        if dry_run:
            exists = self.store.get_claim(mapped.row["id"]) is not None
            outcome = "updated" if exists else "created"
        else:
            outcome = self.store.upsert_claim(mapped.row)
            self.store.link_fhir(source, "Claim", mapped.external_id, mapped.row["id"])
        report.bump(report.updated if outcome == "updated" else report.created, "Claim")

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _patient_reference(resource: dict[str, Any]) -> str | None:
        if resource.get("resourceType") == "Appointment":
            for participant in resource.get("participant") or []:
                actor = (participant or {}).get("actor") or {}
                kind, _ = mapping.reference_id(actor.get("reference"))
                if kind == "Patient":
                    return str(actor.get("reference"))
            return None
        for key in ("patient", "beneficiary"):
            body = resource.get(key)
            if isinstance(body, dict) and body.get("reference"):
                return str(body["reference"])
        return None

    def _resolve_patient(self, logical_id: str | None, index: dict[str, str]) -> str | None:
        if not logical_id:
            return None
        if logical_id in index:
            return index[logical_id]
        # Resources loaded before this feature existed, or a patient keyed by id.
        if self.store.get_patient(logical_id) is not None:
            return logical_id
        return None

    def _patient_index(self, source: str) -> dict[str, str]:
        return {
            link["resource_id"]: link["internal_id"]
            for link in self.store.fhir_links(limit=100_000)
            if link["resource_type"] == "Patient" and link["source"] == source
        }

    def _audit(self, report: IngestReport, *, outcome: str, error: str | None = None) -> None:
        if self.audit is None:
            return
        detail: dict[str, Any] = {
            "source": report.source,
            "dry_run": report.dry_run,
            "created": report.totals["created"],
            "updated": report.totals["updated"],
            "skipped": report.totals["skipped"],
            "warnings": report.totals["warnings"],
            "duration_ms": report.duration_ms,
        }
        if error:
            detail["error"] = error
        self.audit.record(self.principal, action="fhir.sync", outcome=outcome, detail=detail)


def _order_key(resource_type: str) -> int:
    return INGEST_ORDER.index(resource_type) if resource_type in INGEST_ORDER else len(INGEST_ORDER)
