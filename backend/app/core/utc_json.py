import re
from datetime import UTC, datetime
from fastapi.responses import JSONResponse

_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$")
STAMPS={"created_at","updated_at","completed_at","last_synced_at","last_attempt_at","received_at","deleted_at","started_at","finished_at","processed_at","time"}
def utc_json(value,key=None):
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z") if value.tzinfo is None else value.isoformat()
    if isinstance(value, str) and key in STAMPS and _STAMP.fullmatch(value):
        return value + "Z"
    if isinstance(value, dict):
        return {k: utc_json(v,k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [utc_json(v) for v in value]
    return value

class UTCJSONResponse(JSONResponse):
    def render(self, content):
        return super().render(utc_json(content))
