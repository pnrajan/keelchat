from keelchat import ToolError, tool

ORDERS = {"ORD-12345": "shipped", "ORD-67890": "processing"}


@tool("order_status", timeout_s=3.0, retries=1)
async def order_status(order_number: str) -> dict[str, str]:
    """The delivery status of one order, looked up by its order number."""
    status = ORDERS.get(order_number.strip().upper())
    if status is None:
        raise ToolError("order_status", "not_found", "no such order")
    return {"order_number": order_number, "status": status}
