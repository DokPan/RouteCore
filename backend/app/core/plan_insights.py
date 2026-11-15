from datetime import datetime


def describe_plan_changes(previous_plan, current_plan):
    def collect_visits(plan):
        return {
            stop.order_id: (route.staff_id, stop.start, stop.end)
            for route in plan.routes for stop in route.stops
            if stop.type == "VISIT" and stop.status != "CANCELLED"
        } if plan else {}

    previous_visits = collect_visits(previous_plan)
    current_visits = collect_visits(current_plan)
    return [
        {"order_id": order_id, "before": previous_visits.get(order_id), "after": current_visits.get(order_id)}
        for order_id in sorted(previous_visits.keys() | current_visits.keys())
        if previous_plan and previous_visits.get(order_id) != current_visits.get(order_id)
    ]


def locate_incidents(plan, reference, orders=()):
    nodes = {node.access_node_id: node for node in reference.access_nodes}
    registered = {order.order_id: order.registered_at for order in orders}
    return [
        {
            "id": group.group_id, "name": node.name, "position": [node.lat, node.lon],
            "orders": group.order_ids, "registration_minutes": registration_span(group.order_ids, registered),
        }
        for group in plan.pgp_groups
        if (node := nodes.get(group.access_node_id)) is not None
    ]


def registration_span(order_ids, registered):
    times = [datetime.fromisoformat(registered[order_id]) for order_id in order_ids if order_id in registered]
    return round((max(times) - min(times)).total_seconds() / 60) if times else None
