---
name: manage-pet-orders
description: |
  This skill allows an agent to manage pet orders by checking inventory, placing an order, and retrieving order details to confirm the purchase.
allowed-tools:
  - getInventory
  - placeOrder
  - getOrderById
---

## When to use this skill
Use this skill when you need to manage pet orders in the pet store, including checking inventory, placing an order, and confirming the order details.

## Procedure
1. Call the `getInventory` tool to check the current inventory of pets available for order.
2. Based on the inventory, use the `placeOrder` tool to place an order for a specific pet by providing the necessary `id` and `petId` parameters.
3. After placing the order, retrieve the order details by calling the `getOrderById` tool with the `orderId` obtained from the previous step to confirm the purchase.
