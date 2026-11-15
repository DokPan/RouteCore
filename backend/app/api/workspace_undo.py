from dataclasses import dataclass
from time import monotonic
from uuid import uuid4

from .orchestrator import congestion_profiles, db, plan_history, visit_end_overrides
from ..core.distance import distance_engine


UNDO_SECONDS = 10


@dataclass
class WorkspaceSnapshot:
    state: dict
    history: dict
    overrides: dict
    congestion: dict
    comparisons: dict

    @classmethod
    def capture(cls, comparisons):
        state = {key: value.copy() if isinstance(value, dict) else value for key, value in db.items()}
        state["events_log"] = {date: events.copy() for date, events in db["events_log"].items()}
        return cls(state, plan_history.copy(), {date: items.copy() for date, items in visit_end_overrides.items()},
                   congestion_profiles.copy(), comparisons.copy())

    def restore(self, comparisons):
        db.update(self.state)
        distance_engine.set_matrix(db["travel_matrix"])
        for target, saved in (
            (plan_history, self.history), (visit_end_overrides, self.overrides),
            (congestion_profiles, self.congestion), (comparisons, self.comparisons),
        ):
            target.clear()
            target.update(saved)


@dataclass
class WorkspaceUndo:
    token: str
    expires: float
    before: WorkspaceSnapshot
    after: WorkspaceSnapshot

    @classmethod
    def create(cls, before, after):
        return cls(uuid4().hex, monotonic() + UNDO_SECONDS, before, after)
