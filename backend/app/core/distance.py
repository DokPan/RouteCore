import math
from typing import Any, Dict, Optional, Tuple
from ..config import DEFAULT_AVG_SPEED_KMH, DETOUR_FACTOR
from ..models.official_spec import TravelMatrix
from ..constants import RU_TEXT_0018


EKATERINBURG_BRIDGES = [
    {"name": "Мост по ул. Челюскинцев (Северный)", "lat": 56.8580, "lon": 60.6015},
    {"name": "Макаровский мост (ул. Бориса Ельцина)", "lat": 56.8450, "lon": 60.5980},
    {"name": "Каменный мост (ул. Малышева)", "lat": 56.8335, "lon": 60.6050},
    {"name": "Мост по ул. Куйбышева", "lat": 56.8265, "lon": 60.6080},
    {"name": "Мост по ул. Бебеля (Заречный)", "lat": 56.8620, "lon": 60.5520},
]


class DistanceEngine:

    def __init__(self, avg_speed_kmh: float = DEFAULT_AVG_SPEED_KMH, detour_factor: float = DETOUR_FACTOR):
        self.avg_speed_kmh = avg_speed_kmh
        self.detour_factor = detour_factor
        self.matrix: Optional[TravelMatrix] = None
        self._point_indices: Dict[str, int] = {}
        self._cache: Dict[Tuple[float, float, float, float], Tuple[float, int]] = {}
        self.traffic_enabled: bool = False
        self.disabled_hours: set = set()
        self.hourly_factors: Dict[int, float] = {}
        self.bridge_penalty_enabled: bool = False
        self.bridge_peak_factor: float = 1.0

    def set_matrix(self, matrix: Optional[TravelMatrix]):
        self.matrix = matrix
        self._point_indices = {pt: idx for idx, pt in enumerate(matrix.points)} if matrix else {}
        self.hourly_factors = {}
        if matrix and matrix.hourly_factors:
            for hf in matrix.hourly_factors:
                self.hourly_factors[hf.hour] = hf.factor

    def is_crossing_iset_river(self, lat1: float, lon1: float, lat2: float, lon2: float) -> bool:
        if min(lat1, lat2) < 56.800 or max(lat1, lat2) > 56.880:
            return False
        west_to_east = (lon1 < 60.598 and lon2 > 60.605)
        east_to_west = (lon1 > 60.605 and lon2 < 60.598)
        return west_to_east or east_to_west

    def get_hourly_factor(self, hour: Optional[int]) -> float:
        if hour is None or hour in self.disabled_hours:
            return 1.0
        if self.traffic_enabled and hour in self.hourly_factors:
            return self.hourly_factors[hour]
        if self.matrix and self.matrix.hourly_factors:
            for hf in self.matrix.hourly_factors:
                if hf.hour == hour:
                    return hf.factor
        return 1.0

    def get_travel(self, from_id: str, to_id: str, hour: Optional[int] = None) -> Tuple[int, int]:
        if self.matrix and from_id in self._point_indices and to_id in self._point_indices:
            i = self._point_indices[from_id]
            j = self._point_indices[to_id]
            base_sec = self.matrix.durations_sec[i][j]
            dist_m = self.matrix.distances_m[i][j]
            factor = self.get_hourly_factor(hour)
            final_sec = int(round(base_sec * factor))
            return final_sec, dist_m
        return 0, 0

    def calculate_distance_km(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        if abs(lat1 - lat2) < 1e-6 and abs(lon1 - lon2) < 1e-6:
            return 0.0

        r = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
        )
        c = 2 * math.asin(math.sqrt(a))
        straight_km = r * c
        return round(straight_km * self.detour_factor, 2)

    def calculate_travel_time_sec(self, lat1: float, lon1: float, lat2: float, lon2: float, hour: Optional[int] = None) -> int:
        dist_km = self.calculate_distance_km(lat1, lon1, lat2, lon2)
        if dist_km == 0:
            return 0
        hours = dist_km / self.avg_speed_kmh
        base_sec = max(180, int(round(hours * 3600)))

        factor = self.get_hourly_factor(hour) if (self.traffic_enabled and hour is not None) else 1.0
        if self.bridge_penalty_enabled and hour in (8, 9, 17, 18, 19):
            if self.is_crossing_iset_river(lat1, lon1, lat2, lon2):
                factor *= self.bridge_peak_factor

        return int(round(base_sec * factor))

    def calculate_travel_time_min(self, lat1: float, lon1: float, lat2: float, lon2: float, hour: Optional[int] = None) -> int:
        sec = self.calculate_travel_time_sec(lat1, lon1, lat2, lon2, hour=hour)
        return int(math.ceil(sec / 60))

    def get_traffic_profile(self) -> Dict[str, Any]:
        hourly_view = {str(h): self.hourly_factors.get(h, 1.0) for h in range(8, 21)}
        return {
            "city": RU_TEXT_0018,
            "traffic_enabled": self.traffic_enabled,
            "hourly_factors": hourly_view,
            "disabled_hours": sorted(list(self.disabled_hours)),
            "bridge_penalty_enabled": self.bridge_penalty_enabled,
            "bridge_peak_factor": self.bridge_peak_factor,
            "bridges": EKATERINBURG_BRIDGES,
            "peak_hours": [8, 9, 17, 18, 19],
            "algorithm": "T_travel = T_base * K_hour (TravelMatrix)" if not self.bridge_penalty_enabled else "T_travel = T_base * K_hour * K_bridge",
        }


distance_engine = DistanceEngine()
