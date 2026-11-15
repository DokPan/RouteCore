PARTNER_ORDER_PREFIX = "VLS-PARTNER-"
JOINT_VISIT_REVIEW = "Совместный выезд требует повторного назначения обоих мастеров."


def linked_order_ids(order_id):
    original_id = order_id.removeprefix(PARTNER_ORDER_PREFIX)
    return original_id, PARTNER_ORDER_PREFIX + original_id
