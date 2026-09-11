import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def write_status(data_dir, status, filename="health.json", **details):
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    fd, temporary = tempfile.mkstemp(prefix="health-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / filename)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
