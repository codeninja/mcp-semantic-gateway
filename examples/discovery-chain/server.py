from fastmcp import FastMCP
import random

mcp = FastMCP("Discovery-Chain-Demo")

# A set of tools that do NOT exist in the LLM's initial context
# These will be "forged" or retrieved via ToolSearch
# They are category-based tools from the 250-tool stress-test

@mcp.tool()
def billing_summary(user_id: str) -> str:
    """Gets a summary of all invoices for a user."""
    return f"Billing summary for {user_id}: 3 paid, 0 pending, 1 overdue ($45.00)."

@mcp.tool()
def security_scan_status(resource_id: str) -> str:
    """Checks the security status of a cloud resource."""
    return f"Resource {resource_id} is secure. No vulnerabilities found in last 24h."

@mcp.tool()
def tech_support_ticket(id: str) -> str:
    """Gets status of a technical support ticket."""
    return f"Ticket {id} is status: OPEN (Assigned to level 2 support)."

if __name__ == "__main__":
    mcp.run()
