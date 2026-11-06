"""SQLite persistence.

Kept deliberately boring: one connection, one lock, WAL when on disk. The method
surface is the seam you would swap for Postgres - tools never touch SQL.

All seeded data is synthetic. There is no real PHI anywhere in this repository.
"""

from __future__ import annotations

import json
import random
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY,
    mrn TEXT NOT NULL,
    name TEXT NOT NULL,
    dob TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    address TEXT,
    payer TEXT NOT NULL,
    risk_score REAL DEFAULT 0,
    allergies TEXT DEFAULT '[]',
    documents TEXT DEFAULT '[]',
    primary_care TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS beds (
    id TEXT PRIMARY KEY,
    ward TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    patient_id TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS appointments (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    dept TEXT NOT NULL,
    provider TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    status TEXT NOT NULL,
    appt_type TEXT NOT NULL,
    lead_days INTEGER NOT NULL,
    prior_no_shows INTEGER NOT NULL,
    distance_km REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discharges (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    ward TEXT NOT NULL,
    target_date TEXT NOT NULL,
    barriers TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    payer TEXT NOT NULL,
    cpt TEXT NOT NULL,
    icd10 TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    auth_on_file INTEGER NOT NULL,
    submitted_at TEXT,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staff_shifts (
    id TEXT PRIMARY KEY,
    ward TEXT NOT NULL,
    shift TEXT NOT NULL,
    role TEXT NOT NULL,
    assigned INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_by TEXT,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    task TEXT NOT NULL,
    purpose TEXT,
    principal_sub TEXT NOT NULL,
    role TEXT NOT NULL,
    tenant TEXT NOT NULL,
    status TEXT NOT NULL,
    messages TEXT NOT NULL,
    steps TEXT NOT NULL,
    usage TEXT NOT NULL,
    answer TEXT,
    approval_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    tenant TEXT NOT NULL,
    action TEXT NOT NULL,
    tool TEXT,
    outcome TEXT NOT NULL,
    args_hash TEXT,
    detail TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fhir_links (
    source TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    internal_id TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (source, resource_type, resource_id)
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_beds_ward ON beds(ward);
CREATE INDEX IF NOT EXISTS idx_appts_start ON appointments(start_ts);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS idx_fhir_links_internal ON fhir_links(source, resource_type, internal_id);
"""

WARD_SPECS: dict[str, dict[str, Any]] = {
    "ED": {"prefix": "E", "count": 12, "kind": "ed", "acuity": 1.35, "ratio": 4},
    "ICU": {"prefix": "I", "count": 8, "kind": "icu", "acuity": 2.40, "ratio": 2},
    "MED-A": {"prefix": "A", "count": 10, "kind": "med_surg", "acuity": 1.00, "ratio": 5},
    "MED-B": {"prefix": "B", "count": 10, "kind": "med_surg", "acuity": 1.00, "ratio": 5},
    "SURG": {"prefix": "S", "count": 8, "kind": "surgical", "acuity": 1.50, "ratio": 3},
    "PEDS": {"prefix": "P", "count": 6, "kind": "peds", "acuity": 1.20, "ratio": 4},
}

PAYERS = ["Medicare", "Medicaid", "BlueShield", "Aetna", "United", "SelfPay"]
BARRIERS = [
    "awaiting_imaging",
    "awaiting_labs",
    "pt_eval_pending",
    "social_work_clearance",
    "med_recs_reconciliation",
    "transport_arranged",
    "insurance_authorization",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str = ":memory:", *, auto_seed: bool = True) -> None:
        self.path = path
        if path not in {":memory:", ""}:
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if path not in {":memory:", ""}:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        if auto_seed:
            self.seed()

    # -- low level ---------------------------------------------------------
    def _q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def _one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(params))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- seed --------------------------------------------------------------
    def seed(self, reset: bool = False) -> None:
        with self._lock:
            if reset:
                for table in ("patients", "beds", "appointments", "discharges", "claims", "staff_shifts"):
                    self._conn.execute(f"DELETE FROM {table}")
                self._conn.commit()
            if self._one("SELECT COUNT(*) AS n FROM patients")["n"] > 0:
                return
            self._seed_patients()
            self._seed_beds()
            self._seed_appointments()
            self._seed_discharges()
            self._seed_claims()
            self._seed_shifts()
            self._conn.commit()

    def _seed_patients(self) -> None:
        rng = random.Random(7)
        rows = []
        for i in range(1, 45):
            pid = f"P{i:04d}"
            docs = ["id_proof", "insurance_card"]
            if i % 3 == 0:
                docs.append("referral")
            if i % 4 == 0:
                docs.append("prior_auth")
            rows.append(
                (
                    pid,
                    f"{100000 + i}",
                    f"Test Patient {i:02d}",
                    f"{1935 + (i * 7) % 60}-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}",
                    f"555-01{i:02d}",
                    f"patient{i:02d}@example.org",
                    f"{100 + i} Example Street, Testville",
                    PAYERS[i % len(PAYERS)],
                    round(rng.uniform(0.1, 0.95), 2),
                    json.dumps(["penicillin"] if i % 5 == 0 else []),
                    json.dumps(docs),
                    f"Dr. Example {(i % 6) + 1}",
                    utcnow(),
                )
            )
        self._conn.executemany(
            "INSERT INTO patients (id,mrn,name,dob,phone,email,address,payer,risk_score,"
            "allergies,documents,primary_care,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

    def _seed_beds(self) -> None:
        # Occupancy pattern: ED tight with boarders, ICU near full, med-surg ~80%.
        plan = {
            "ED": [("occupied", 8), ("boarding", 3), ("free", 1)],
            "ICU": [("occupied", 7), ("blocked", 1)],
            "MED-A": [("occupied", 8), ("free", 2)],
            "MED-B": [("occupied", 9), ("cleaning", 1)],
            "SURG": [("occupied", 6), ("free", 2)],
            "PEDS": [("occupied", 3), ("free", 3)],
        }
        pid = 1
        now = utcnow()
        rows = []
        for ward, spec in WARD_SPECS.items():
            idx = 0
            for status, count in plan[ward]:
                for _ in range(count):
                    idx += 1
                    bed_id = f"{spec['prefix']}{idx:02d}"
                    patient_id = None
                    if status in {"occupied", "boarding"}:
                        patient_id = f"P{pid:04d}"
                        pid += 1
                    rows.append((bed_id, ward, spec["kind"], status, patient_id, now))
        self._conn.executemany(
            "INSERT INTO beds (id,ward,kind,status,patient_id,updated_at) VALUES (?,?,?,?,?,?)", rows
        )

    def _seed_appointments(self) -> None:
        depts = ["Cardiology", "Orthopedics", "Oncology", "Primary Care", "Neurology", "Imaging"]
        providers = ["Dr. Rao", "Dr. Chen", "Dr. Okafor", "Dr. Silva"]
        rng = random.Random(11)
        base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        rows = []
        for i in range(1, 15):
            lead = [1, 2, 3, 7, 14, 21, 30, 45][i % 8]
            rows.append(
                (
                    f"A{i:04d}",
                    f"P{i:04d}",
                    depts[i % len(depts)],
                    providers[i % len(providers)],
                    (base + timedelta(days=lead, hours=9 + (i % 6))).isoformat(timespec="seconds"),
                    "scheduled",
                    "new" if i % 3 == 0 else "follow_up",
                    lead,
                    rng.choice([0, 0, 0, 1, 2]),
                    round(rng.uniform(2, 55), 1),
                    utcnow(),
                )
            )
        self._conn.executemany(
            "INSERT INTO appointments (id,patient_id,dept,provider,start_ts,status,appt_type,"
            "lead_days,prior_no_shows,distance_km,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

    def _seed_discharges(self) -> None:
        rng = random.Random(23)
        today = datetime.now(timezone.utc).date()
        rows = []
        candidates = [  # (patient, ward) - must match the bed assignment above
            ("P0019", "MED-A"), ("P0020", "MED-A"), ("P0022", "MED-A"),
            ("P0028", "MED-B"), ("P0030", "MED-B"), ("P0033", "MED-B"),
            ("P0037", "SURG"), ("P0039", "SURG"), ("P0040", "SURG"), ("P0041", "SURG"),
        ]
        for i, (pid, ward) in enumerate(candidates, start=1):
            picked = rng.sample(BARRIERS, k=rng.randint(0, 3))
            rows.append(
                (
                    f"D{i:04d}",
                    pid,
                    ward,
                    (today + timedelta(days=rng.choice([0, 0, 0, 1, 2]))).isoformat(),
                    json.dumps(picked),
                    "planned",
                    utcnow(),
                )
            )
        self._conn.executemany(
            "INSERT INTO discharges (id,patient_id,ward,target_date,barriers,status,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            rows,
        )

    def _seed_claims(self) -> None:
        rows = []
        cpt_icd = [
            ("99213", "I10", 12_500, 1),
            ("99214", "E11.9", 18_000, 1),
            ("70551", "R51.9", 145_000, 0),
            ("27447", "M17.11", 3_250_000, 1),
            ("93000", "I25.10", 9_000, 1),
            ("97110", "M54.5", 11_000, 0),
            ("73721", "M25.561", 98_000, 1),
            ("99215", "J44.1", 24_000, 1),
            ("43239", "K21.9", 210_000, 0),
            ("36415", "Z00.00", 3_500, 1),
            ("87635", "U07.1", 14_000, 1),
            ("20610", "M17.12", 22_000, 0),
        ]
        for i, (cpt, icd, cents, auth) in enumerate(cpt_icd, start=1):
            rows.append(
                (
                    f"C{i:04d}",
                    f"P{i:04d}",
                    PAYERS[i % len(PAYERS)],
                    cpt,
                    icd,
                    cents,
                    auth,
                    utcnow(),
                    "draft",
                )
            )
        self._conn.executemany(
            "INSERT INTO claims (id,patient_id,payer,cpt,icd10,amount_cents,auth_on_file,"
            "submitted_at,status) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )

    def _seed_shifts(self) -> None:
        now = utcnow()
        day_rn = {"ED": 6, "ICU": 7, "MED-A": 3, "MED-B": 3, "SURG": 4, "PEDS": 2}
        # Night rosters are deliberately thinner: two real gaps for the agent to find.
        night_rn = {"ED": 4, "ICU": 5, "MED-A": 2, "MED-B": 2, "SURG": 3, "PEDS": 2}
        md = {"ED": 2, "ICU": 1, "MED-A": 1, "MED-B": 1, "SURG": 2, "PEDS": 1}
        rows = []
        for ward in WARD_SPECS:
            rows.append((f"{ward}-day-RN", ward, "day", "RN", day_rn[ward], now))
            rows.append((f"{ward}-night-RN", ward, "night", "RN", night_rn[ward], now))
            rows.append((f"{ward}-day-MD", ward, "day", "MD", md[ward], now))
            rows.append((f"{ward}-night-MD", ward, "night", "MD", md[ward], now))
        self._conn.executemany(
            "INSERT INTO staff_shifts (id,ward,shift,role,assigned,updated_at) VALUES (?,?,?,?,?,?)",
            rows,
        )

    # -- patients ----------------------------------------------------------
    def get_patient(self, patient_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM patients WHERE id = ?", (patient_id,))
        return _decode_patient(row) if row else None

    def search_patients(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        like = f"%{query.strip()}%"
        rows = self._q(
            "SELECT * FROM patients WHERE id LIKE ? OR mrn LIKE ? OR name LIKE ? ORDER BY id LIMIT ?",
            (like, like, like, limit),
        )
        return [_decode_patient(r) for r in rows]

    # -- beds --------------------------------------------------------------
    def beds(self, ward: str | None = None) -> list[dict[str, Any]]:
        if ward:
            rows = self._q("SELECT * FROM beds WHERE ward = ? ORDER BY id", (ward,))
        else:
            rows = self._q("SELECT * FROM beds ORDER BY ward, id")
        return [dict(r) for r in rows]

    def get_bed(self, bed_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM beds WHERE id = ?", (bed_id,))
        return dict(row) if row else None

    def assign_bed(self, patient_id: str, bed_id: str) -> dict[str, Any]:
        bed = self.get_bed(bed_id)
        if bed is None:
            raise KeyError(f"bed {bed_id} not found")
        if bed["status"] not in {"free", "cleaning"}:
            raise ValueError(f"bed {bed_id} is {bed['status']}, not available")
        current = self._one("SELECT id FROM beds WHERE patient_id = ?", (patient_id,))
        with self._lock:
            if current:
                self._conn.execute(
                    "UPDATE beds SET status='cleaning', patient_id=NULL, updated_at=? WHERE id=?",
                    (_now(), current["id"]),
                )
            self._conn.execute(
                "UPDATE beds SET status='occupied', patient_id=?, updated_at=? WHERE id=?",
                (patient_id, _now(), bed_id),
            )
            self._conn.commit()
        return self.get_bed(bed_id) or {}

    # -- appointments ------------------------------------------------------
    def appointments(
        self,
        *,
        dept: str | None = None,
        provider: str | None = None,
        status: str | None = "scheduled",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM appointments WHERE 1=1"
        params: list[Any] = []
        if dept:
            sql += " AND dept = ?"
            params.append(dept)
        if provider:
            sql += " AND provider = ?"
            params.append(provider)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY start_ts LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._q(sql, params)]

    def get_appointment(self, appointment_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM appointments WHERE id = ?", (appointment_id,))
        return dict(row) if row else None

    def create_appointment(self, payload: dict[str, Any]) -> dict[str, Any]:
        appt_id = payload["id"]
        self._exec(
            "INSERT INTO appointments (id,patient_id,dept,provider,start_ts,status,appt_type,"
            "lead_days,prior_no_shows,distance_km,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                appt_id,
                payload["patient_id"],
                payload["dept"],
                payload["provider"],
                payload["start_ts"],
                "scheduled",
                payload.get("appt_type", "new"),
                int(payload.get("lead_days", 0)),
                int(payload.get("prior_no_shows", 0)),
                float(payload.get("distance_km", 0)),
                utcnow(),
            ),
        )
        row = self._one("SELECT * FROM appointments WHERE id = ?", (appt_id,))
        return dict(row) if row else {}

    # -- discharges --------------------------------------------------------
    def discharges(self, ward: str | None = None) -> list[dict[str, Any]]:
        if ward:
            rows = self._q("SELECT * FROM discharges WHERE ward = ? ORDER BY target_date", (ward,))
        else:
            rows = self._q("SELECT * FROM discharges ORDER BY target_date")
        out = []
        for row in rows:
            item = dict(row)
            item["barriers"] = json.loads(item["barriers"])
            out.append(item)
        return out

    # -- claims ------------------------------------------------------------
    def claims(self, *, payer: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM claims WHERE 1=1"
        params: list[Any] = []
        if payer:
            sql += " AND payer = ?"
            params.append(payer)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id"
        return [dict(r) for r in self._q(sql, params)]

    def get_claim(self, claim_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM claims WHERE id = ?", (claim_id,))
        return dict(row) if row else None

    # -- staffing ----------------------------------------------------------
    def shifts(self, ward: str | None = None) -> list[dict[str, Any]]:
        if ward:
            rows = self._q("SELECT * FROM staff_shifts WHERE ward = ? ORDER BY shift, role", (ward,))
        else:
            rows = self._q("SELECT * FROM staff_shifts ORDER BY ward, shift, role")
        return [dict(r) for r in rows]

    # -- external identity (FHIR) -----------------------------------------
    # Upserts are field-scoped on purpose. FHIR is authoritative for the fields
    # it owns; risk_score, documents, prior_no_shows and distance_km are local
    # state and must survive a re-sync untouched.
    def upsert_patient(self, row: dict[str, Any]) -> str:
        payload = (
            row["mrn"],
            row["name"],
            row["dob"],
            row.get("phone") or "",
            row.get("email") or "",
            row.get("address") or "",
        )
        exists = self._one("SELECT id FROM patients WHERE id = ?", (row["id"],)) is not None
        if exists:
            self._exec(
                "UPDATE patients SET mrn=?,name=?,dob=?,phone=?,email=?,address=? WHERE id=?",
                (*payload, row["id"]),
            )
        else:
            self._exec(
                "INSERT INTO patients (id,mrn,name,dob,phone,email,address,payer,risk_score,"
                "allergies,documents,primary_care,created_at) VALUES (?,?,?,?,?,?,?,?,0,'[]','[]',?,?)",
                (row["id"], *payload, row.get("payer") or "", None, utcnow()),
            )
        return "updated" if exists else "created"

    def patch_patient_payer(self, patient_id: str, payer: str) -> bool:
        if not payer:
            return False
        updated = self._one("SELECT id FROM patients WHERE id = ?", (patient_id,))
        if updated is None:
            return False
        self._exec("UPDATE patients SET payer=? WHERE id=?", (payer, patient_id))
        return True

    def upsert_appointment(self, row: dict[str, Any]) -> str:
        exists = self._one("SELECT id FROM appointments WHERE id = ?", (row["id"],)) is not None
        if exists:
            self._exec(
                "UPDATE appointments SET patient_id=?,dept=?,provider=?,start_ts=?,status=?,appt_type=?,"
                "lead_days=? WHERE id=?",
                (
                    row["patient_id"],
                    row["dept"],
                    row["provider"],
                    row["start_ts"],
                    row["status"],
                    row["appt_type"],
                    int(row.get("lead_days", 0)),
                    row["id"],
                ),
            )
        else:
            self._exec(
                "INSERT INTO appointments (id,patient_id,dept,provider,start_ts,status,appt_type,"
                "lead_days,prior_no_shows,distance_km,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["patient_id"],
                    row["dept"],
                    row["provider"],
                    row["start_ts"],
                    row["status"],
                    row["appt_type"],
                    int(row.get("lead_days", 0)),
                    0,
                    0.0,
                    utcnow(),
                ),
            )
        return "updated" if exists else "created"

    def upsert_claim(self, row: dict[str, Any]) -> str:
        exists = self._one("SELECT id FROM claims WHERE id = ?", (row["id"],)) is not None
        values = (
            row["patient_id"],
            row["payer"],
            row["cpt"],
            row["icd10"],
            int(row["amount_cents"]),
            int(row["auth_on_file"]),
            row.get("submitted_at"),
            row["status"],
        )
        if exists:
            self._exec(
                "UPDATE claims SET patient_id=?,payer=?,cpt=?,icd10=?,amount_cents=?,auth_on_file=?,"
                "submitted_at=?,status=? WHERE id=?",
                (*values, row["id"]),
            )
        else:
            self._exec(
                "INSERT INTO claims (id,patient_id,payer,cpt,icd10,amount_cents,auth_on_file,"
                "submitted_at,status) VALUES (?,?,?,?,?,?,?,?,?)",
                (row["id"], *values),
            )
        return "updated" if exists else "created"

    # -- FHIR links --------------------------------------------------------
    def link_fhir(self, source: str, resource_type: str, resource_id: str, internal_id: str) -> None:
        self._exec(
            "INSERT INTO fhir_links (source,resource_type,resource_id,internal_id,synced_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(source,resource_type,resource_id) DO UPDATE SET "
            "internal_id=excluded.internal_id, synced_at=excluded.synced_at",
            (source, resource_type, resource_id, internal_id, utcnow()),
        )

    def internal_for_fhir(self, source: str, resource_type: str, resource_id: str) -> str | None:
        row = self._one(
            "SELECT internal_id FROM fhir_links WHERE source=? AND resource_type=? AND resource_id=?",
            (source, resource_type, resource_id),
        )
        return row["internal_id"] if row else None

    def external_for_internal(self, source: str, resource_type: str, internal_id: str) -> str | None:
        row = self._one(
            "SELECT resource_id FROM fhir_links WHERE source=? AND resource_type=? AND internal_id=? "
            "ORDER BY synced_at DESC LIMIT 1",
            (source, resource_type, internal_id),
        )
        return row["resource_id"] if row else None

    def fhir_links(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM fhir_links ORDER BY synced_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # -- approvals ---------------------------------------------------------
    def create_approval(self, record: dict[str, Any]) -> dict[str, Any]:
        self._exec(
            "INSERT INTO approvals (id,run_id,tool,args,requested_by,status,created_at,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                record["id"],
                record["run_id"],
                record["tool"],
                json.dumps(record["args"], sort_keys=True),
                record["requested_by"],
                "pending",
                record["created_at"],
                record["expires_at"],
            ),
        )
        return self.get_approval(record["id"]) or {}

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if not row:
            return None
        item = dict(row)
        item["args"] = json.loads(item["args"])
        return item

    def pending_approvals(self) -> list[dict[str, Any]]:
        """Approvals still awaiting a decision, plus approved ones awaiting execution."""
        rows = self._q(
            "SELECT id FROM approvals WHERE status IN ('pending','approved') ORDER BY created_at"
        )
        return [self.get_approval(r["id"]) for r in rows]  # type: ignore[misc]

    def set_approval_status(self, approval_id: str, status: str) -> None:
        self._exec("UPDATE approvals SET status=? WHERE id=?", (status, approval_id))

    def decide_approval(self, approval_id: str, *, decision: str, decided_by: str, reason: str) -> dict[str, Any]:
        approval = self.get_approval(approval_id)
        if approval is None:
            raise KeyError(f"approval {approval_id} not found")
        if approval["status"] != "pending":
            raise ValueError(f"approval {approval_id} already {approval['status']}")
        self._exec(
            "UPDATE approvals SET status=?, decided_by=?, reason=? WHERE id=?",
            (decision, decided_by, reason, approval_id),
        )
        return self.get_approval(approval_id) or {}

    # -- runs --------------------------------------------------------------
    def save_run(self, record: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO runs (id,session_id,task,purpose,principal_sub,role,tenant,status,"
            "messages,steps,usage,answer,approval_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, messages=excluded.messages, "
            "steps=excluded.steps, usage=excluded.usage, answer=excluded.answer, "
            "approval_id=excluded.approval_id, updated_at=excluded.updated_at",
            (
                record["id"],
                record.get("session_id"),
                record["task"],
                record.get("purpose"),
                record["principal_sub"],
                record["role"],
                record["tenant"],
                record["status"],
                json.dumps(record.get("messages", [])),
                json.dumps(record.get("steps", [])),
                json.dumps(record.get("usage", {})),
                record.get("answer"),
                record.get("approval_id"),
                record["created_at"],
                utcnow(),
            ),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not row:
            return None
        item = dict(row)
        for key in ("messages", "steps", "usage"):
            item[key] = json.loads(item[key])
        return item

    # -- audit -------------------------------------------------------------
    def last_audit_hash(self) -> str | None:
        row = self._one("SELECT hash FROM audit ORDER BY id DESC LIMIT 1")
        return row["hash"] if row else None

    def insert_audit(self, payload: dict[str, Any], *, prev_hash: str, hash_value: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO audit (ts,actor,role,tenant,action,tool,outcome,args_hash,detail,"
                "prev_hash,hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["ts"],
                    payload["actor"],
                    payload["role"],
                    payload["tenant"],
                    payload["action"],
                    payload["tool"],
                    payload["outcome"],
                    payload["args_hash"],
                    json.dumps(payload["detail"], sort_keys=True),
                    prev_hash,
                    hash_value,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0:
            rows = self._q("SELECT * FROM audit ORDER BY id")
        else:
            rows = self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            out.append(item)
        return out

    # -- idempotency -------------------------------------------------------
    def get_idempotent(self, key: str) -> dict[str, Any] | None:
        row = self._one("SELECT response FROM idempotency WHERE key = ?", (key,))
        return json.loads(row["response"]) if row else None

    def put_idempotent(self, key: str, tool: str, response: dict[str, Any]) -> None:
        try:
            self._exec(
                "INSERT INTO idempotency (key,tool,response,created_at) VALUES (?,?,?,?)",
                (key, tool, json.dumps(response, default=str), utcnow()),
            )
        except sqlite3.IntegrityError:  # concurrent duplicate; first writer wins
            pass


def _decode_patient(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["allergies"] = json.loads(item["allergies"] or "[]")
    item["documents"] = json.loads(item["documents"] or "[]")
    return item
