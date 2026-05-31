from dataclasses import dataclass
from typing import Literal

SourceType = Literal["gmail_thread", "google_doc", "whatsapp_export"]
# "no_new_rows" == a clean run that fetched input but stored nothing new (an
# empty delta, e.g. a SENT-only nightly window). It is a SUCCESS, not a failure:
# consumers must not exit nonzero / mark the step failed on it (b169).
IngestionStatus = Literal[
    "stub", "completed", "completed_with_warnings", "no_new_rows", "failed"
]


@dataclass(slots=True)
class IngestionResult:
    source_type: SourceType
    status: IngestionStatus
    detail: str
    run_id: str | None = None
