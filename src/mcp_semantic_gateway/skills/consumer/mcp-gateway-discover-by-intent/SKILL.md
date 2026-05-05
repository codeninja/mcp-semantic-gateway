---
name: mcp-gateway-discover-by-intent
description: Use when the mcp-semantic-gateway is connected and the agent needs to find the right tool or workflow for a user request — refunding an order, onboarding a record, triaging an issue, etc. Establishes the search-before-guess pattern. Trigger on any task where the obvious tool name is not already in the agent's context.
---

# Discover capability by intent, not by tool name

The gateway does not preload every upstream tool into your context. Instead it gives you four meta-tools and expects you to **search before you guess**. Tool names are bad search keys; descriptions of *what the user is trying to do* are good ones.

## The four gateway tools

| Tool | When to call it |
|---|---|
| `mcp_semantic_gateway_find_skills` | First. Search for a workflow that matches the user's intent. |
| `mcp_semantic_gateway_get_skill` | After `find_skills` returns a candidate whose description fits. Pulls the full procedural body and the exact tool list. |
| `mcp_semantic_gateway_find_prompts` | Use for prompt templates (less common). |
| `mcp_semantic_gateway_context` | Set or refine the retrieval context for the rest of the turn. |

## The procedure

1. **Read the user's request as an intent.** *"refund Sara's last order"*, *"onboard a new pet named Rex"*, *"close out yesterday's tickets"*. Phrase queries the way a user would, not the way an API would.
2. **Call `find_skills` with that intent as the query.** Pass the verb and the noun. Skip jargon the upstream API uses unless you already know it applies.
3. **Inspect the candidate descriptions.** They are short on purpose — pick the one whose `description` actually matches the user's task. If none match, broaden the query and search again. Do not invent a tool call from the names alone.
4. **Call `get_skill` on the chosen candidate.** The body lists the tools to use, the order, the inputs, and the failure modes. Treat it as authoritative — the gateway has already verified those tool names exist upstream.
5. **Execute the workflow.** Call the listed upstream tools directly. They are routed to the right server transparently.

## Anti-patterns to avoid

- **Guessing tool names.** If you have not seen `createPet` in this conversation, do not call it. Search first.
- **Skipping `get_skill`.** The `find_skills` description is a teaser, not a procedure. The body is where the tool list lives.
- **Re-searching for tools you already know about.** If a skill body said to call `listOrders` then `refundOrder`, just call them — they are real upstream tools, exposed transparently.
- **Asking for clarification when search would answer.** A `find_skills` round-trip is cheaper than a user round-trip.

## Worked example

User: *"close out yesterday's pending orders"*

```
→ find_skills({"query": "close out pending orders from yesterday"})
← [{"name": "close-pending-orders", "description": "Sweep open orders older than 24h and mark them resolved or escalate."}]
→ get_skill({"name": "close-pending-orders"})
← (procedural body listing listOrders → updateOrderStatus → notifyCustomer)
→ listOrders({"status": "pending", "before": "2026-05-04T00:00:00Z"})
→ updateOrderStatus({...})
```

Three calls to discover, one workflow executed. The agent never had to load 400 unrelated tool definitions.
