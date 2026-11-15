import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import HTTPException
from pydantic import BaseModel, Field, TypeAdapter

class CompletedWork(BaseModel):
    work_type_code: str
    technology: str
    actual_duration_min: int = Field(gt=0, le=1440)


HISTORY_UNAVAILABLE = "Не удалось прочитать или сохранить историю работ."
HISTORY_PATH = Path(os.getenv("ROUTECORE_STATE_DIR", ".routecore-state")) / "duration-history.json"
history_format = TypeAdapter(list[CompletedWork])


def load_duration_history(samples: list[CompletedWork]):
    temporary_path = None
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=HISTORY_PATH.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(history_format.dump_json(samples))
        temporary_path.replace(HISTORY_PATH)
    except OSError:
        raise HTTPException(503, HISTORY_UNAVAILABLE) from None
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)
    return {"loaded": len(samples)}


def read_duration_history():
    try:
        return history_format.validate_json(HISTORY_PATH.read_bytes())
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        raise HTTPException(503, HISTORY_UNAVAILABLE) from None
