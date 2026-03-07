import random
from fastmcp import FastMCP
from faker import Faker

# Create an MCP server
mcp = FastMCP("ToolSearch-Stress-Demo")
fake = Faker()

def create_server():
    # Define 25 categories for our tools
    CATEGORIES = [
        "Greetings", "Apologies", "Encouragement", "Technical_Support", "Sales",
        "Customer_Service", "Product_Launch", "Bug_Report", "Feature_Request", "Billing",
        "Security", "Infrastructure", "Marketing", "Human_Resources", "Legal",
        "Feedback", "Networking", "Database", "Frontend", "Backend",
        "Mobile", "Testing", "DevOps", "Documentation", "Analytics"
    ]

    for cat in CATEGORIES:
        for i in range(1, 11):
            tool_name = f"{cat.lower()}_phrase_{i}"
            
            # Using a closure to capture category and index
            def make_phrase_tool(c, idx):
                @mcp.tool(name=f"{c.lower()}_phrase_{idx}")
                def phrase_tool(name: str = "Dallas") -> str:
                    """
                    Returns a specific category-based phrase.
                    Use this tool when you need a phrase related to category {c}.
                    """
                    return f"Phrase for {c}: {fake.sentence()} (ID: {idx})"
                return phrase_tool
            
            make_phrase_tool(cat, i)

# Initialize 250 tools
create_server()

if __name__ == "__main__":
    mcp.run()
