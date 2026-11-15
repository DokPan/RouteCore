from collections import defaultdict
from datetime import datetime, timedelta

from ..config import PGP_THRESHOLD_ORDERS, PGP_WINDOW_MINUTES
from ..models.official_spec import PgpGroup


DEFAULT_RELEVANT_CODES = frozenset({"REPAIR", "DEGRADATION"})


class PGPDetector:
    def __init__(self, threshold=PGP_THRESHOLD_ORDERS, window_minutes=PGP_WINDOW_MINUTES):
        self.threshold = threshold
        self.window = timedelta(minutes=window_minutes)

    def detect_and_filter(self, orders, as_of, work_types=()):
        now = datetime.fromisoformat(as_of)
        relevant = set(DEFAULT_RELEVANT_CODES)
        for work_type in work_types:
            if work_type.pgp_relevant is True:
                relevant.add(work_type.code)
            elif work_type.pgp_relevant is False:
                relevant.discard(work_type.code)

        by_node = defaultdict(list)
        for order in orders:
            if order.access_node_id and order.work_type_code in relevant:
                registered = datetime.fromisoformat(order.registered_at)
                if registered <= now:
                    by_node[order.access_node_id].append((registered, order.order_id))

        groups = []
        grouped_ids = set()
        for node_id, registrations in sorted(by_node.items()):
            registrations.sort()
            left = 0
            cluster = []
            for right, (registered, _) in enumerate(registrations):
                while registrations[left][0] < registered - self.window:
                    left += 1
                if right - left + 1 < self.threshold:
                    continue
                if cluster and left > cluster[-1]:
                    groups.append(self.make_group(node_id, registrations, cluster, as_of))
                    cluster = []
                first_new = max(left, cluster[-1] + 1) if cluster else left
                cluster.extend(range(first_new, right + 1))
            if cluster:
                groups.append(self.make_group(node_id, registrations, cluster, as_of))

        for group in groups:
            grouped_ids.update(group.order_ids)
        return [order for order in orders if order.order_id not in grouped_ids], groups

    @staticmethod
    def make_group(node_id, registrations, cluster, as_of):
        order_ids = [registrations[index][1] for index in cluster]
        return PgpGroup(
            group_id=f"PGP-{node_id}-{order_ids[0]}", access_node_id=node_id,
            order_ids=order_ids, detected_at=as_of, action="FORWARD_TO_STATION",
        )


pgp_detector = PGPDetector()
