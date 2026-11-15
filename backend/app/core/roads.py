import os
from functools import lru_cache

import httpx


OSRM_URL = os.getenv("OSRM_URL", "").rstrip("/")
BLOCK_SIZE = 50


class RoadServiceError(RuntimeError):
    pass


def request_osrm(client, service, points, **params):
    coordinates = ";".join(f"{lon},{lat}" for lat, lon in points)
    try:
        response = client.get(
            f"{OSRM_URL}/{service}/v1/driving/{coordinates}",
            params={"exclude": "ferry", **params},
        )
        data = response.json()
        if data.get("code") in {"NoRoute", "NoSegment"} and service == "route":
            return None
        response.raise_for_status()
        if data.get("code") == "Ok":
            return data
    except (httpx.HTTPError, ValueError) as exc:
        raise RoadServiceError("OSRM is unavailable") from exc
    raise RoadServiceError("OSRM could not calculate road routes")


@lru_cache(maxsize=2)
def road_table(points):
    blocks = [points[i:i + BLOCK_SIZE] for i in range(0, len(points), BLOCK_SIZE)]
    costs = {}
    with httpx.Client(timeout=10, trust_env=False) as client:
        for sources in blocks:
            for destinations in blocks:
                offset = len(sources)
                data = request_osrm(
                    client, "table", sources + destinations,
                    annotations="duration,distance",
                    sources=";".join(map(str, range(offset))),
                    destinations=";".join(map(str, range(offset, offset + len(destinations)))),
                    skip_waypoints="true",
                )
                for i, origin in enumerate(sources):
                    for j, target in enumerate(destinations):
                        duration = data["durations"][i][j]
                        distance = data["distances"][i][j]
                        costs[origin, target] = (
                            None if duration is None or distance is None
                            else (round(duration), round(distance))
                        )
    return costs


@lru_cache(maxsize=512)
def road_geometry(points):
    with httpx.Client(timeout=5, trust_env=False) as client:
        result = request_osrm(
            client, "route", points, geometries="geojson", overview="full",
        )
    if result is None:
        return [], []
    line = [[lat, lon] for lon, lat in result["routes"][0]["geometry"]["coordinates"]]
    waypoints = [tuple(reversed(waypoint["location"])) for waypoint in result["waypoints"]]
    return line, waypoints
