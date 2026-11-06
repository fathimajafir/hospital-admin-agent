"""FHIR R4 integration.

Three layers, deliberately separated so each can be tested without the others:

* ``client``  - HTTP only. Talks to a FHIR server, returns plain resources.
* ``mapping`` - pure functions. FHIR resource in, internal row out. No I/O.
* ``ingest``  - orchestration. Pulls, maps, upserts, reports what it skipped.

``writeback`` is the narrow outbound path: one appointment, after approval.
"""

from .client import FhirClient
from .errors import FhirError, FhirNotConfigured
from .ingest import FhirIngest, IngestReport
from .mapping import MappingError

__all__ = [
    "FhirClient",
    "FhirError",
    "FhirIngest",
    "FhirNotConfigured",
    "IngestReport",
    "MappingError",
]
