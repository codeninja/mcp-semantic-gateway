---
name: handle-order-cancellation
description: |
  This skill manages the cancellation of a purchase order by retrieving the order details and then proceeding to cancel it if necessary.
allowed-tools:
  - getOrderById
  - deleteOrder
---

## When to use this skill
Use this skill when you need to handle order cancellations in the pet store system. It retrieves the order details using the order ID and cancels the order if required.

## Procedure
1. Call the `getOrderById` tool with the appropriate `orderId` to retrieve the order details.
2. Evaluate the order details to determine if cancellation is necessary.
3. If cancellation is needed, call the `deleteOrder` tool with the same `orderId` to cancel the purchase order.
