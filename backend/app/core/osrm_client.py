import os
import math
import time
import json
import logging
import urllib.request
import urllib.error
from typing import List, Tuple, Dict, Any, Optional
from ..constants import RU_TEXT_0019

logger = logging.getLogger(__name__)

class OSRMClient:

    def __init__(self, base_url: Optional[str] = None, timeout_sec: float = 3.0):

        env_url = os.environ.get("OSRM_URL")
        self.base_url = (env_url or base_url or "http://router.project-osrm.org").rstrip("/")
        self.timeout_sec = timeout_sec
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_max_size = 500

    def get_route_geometry(self, points: List[Tuple[float, float]]) -> Dict[str, Any]:
        if not points or len(points) < 2:
            return {
                "coordinates": points,
                "distance_m": 0,
                "duration_sec": 0,
                "provider": "empty"
            }

        cache_key = "|".join(f"{p[0]:.5f},{p[1]:.5f}" for p in points)
        if cache_key in self._cache:
            return self._cache[cache_key]


        try:

            url_coords = ";".join(f"{p[1]:.5f},{p[0]:.5f}" for p in points)
            url = f"{self.base_url}/route/v1/driving/{url_coords}?overview=full&geometries=geojson&steps=false"

            req = urllib.request.Request(
                url,
                headers={"User-Agent": "RTC-Crew-SmartDispatcher/1.0"}
            )
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as response:
                data = json.loads(response.read().decode("utf-8"))

            if data.get("code") == "Ok" and data.get("routes"):
                route = data["routes"][0]
                raw_coords = route["geometry"]["coordinates"]

                leaflet_coords = [[pt[1], pt[0]] for pt in raw_coords]

                result = {
                    "coordinates": leaflet_coords,
                    "distance_m": int(round(route.get("distance", 0))),
                    "duration_sec": int(round(route.get("duration", 0))),
                    "provider": "OSRM (OpenStreetMap)"
                }
                self._save_to_cache(cache_key, result)
                return result

        except Exception as e:
            logger.warning("OSRM query failed or timed out: %s. Using local road grid fallback.", e)


        result = self._generate_offline_grid_route(points)
        self._save_to_cache(cache_key, result)
        return result

    def _save_to_cache(self, key: str, value: Dict[str, Any]):
        if len(self._cache) >= self._cache_max_size:

            keys_to_remove = list(self._cache.keys())[:len(self._cache) // 4]
            for k in keys_to_remove:
                del self._cache[k]
        self._cache[key] = value

    def _generate_offline_grid_route(self, points: List[Tuple[float, float]]) -> Dict[str, Any]:
        path: List[List[float]] = []
        total_dist_m = 0

        for i in range(len(points) - 1):
            p1 = points[i]
            p2 = points[i + 1]

            lat1, lon1 = p1[0], p1[1]
            lat2, lon2 = p2[0], p2[1]


            if not path:
                path.append([lat1, lon1])


            mid_lat = lat1
            mid_lon = lon2


            if (lon1 < 60.598 and lon2 > 60.605) or (lon1 > 60.605 and lon2 < 60.598):

                bridge_lats = [56.8580, 56.8380, 56.8330, 56.8260]
                closest_bridge_lat = min(bridge_lats, key=lambda b: abs(b - (lat1 + lat2) / 2))

                path.append([closest_bridge_lat, lon1])
                path.append([closest_bridge_lat, lon2])
            else:
                path.append([mid_lat, mid_lon])

            path.append([lat2, lon2])


            dlat = (lat2 - lat1) * 111000
            dlon = (lon2 - lon1) * 111000 * math.cos(math.radians(lat1))
            seg_dist = math.sqrt(dlat ** 2 + dlon ** 2) * 1.3
            total_dist_m += int(round(seg_dist))


        speed_mps = 32.0 * 1000 / 3600
        duration_sec = int(round(total_dist_m / speed_mps)) if speed_mps > 0 else 0

        return {
            "coordinates": path,
            "distance_m": total_dist_m,
            "duration_sec": max(120, duration_sec),
            "provider": RU_TEXT_0019
        }

osrm_client = OSRMClient()
