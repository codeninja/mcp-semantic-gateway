from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Existing API Server")

class Item(BaseModel):
    id: int
    name: str
    category: str

# Our "Benign" endpoints
@app.get("/items/greetings")
def get_greeting():
    """Return a greeting message."""
    return {"message": "Hello from the REST API!"}

@app.get("/items/billing")
def get_billing():
    """Return a billing status."""
    return {"status": "all invoices paid", "amount": 0.0}

@app.get("/items/security")
def get_security():
    """Return security status."""
    return {"status": "secure", "threat_level": "low"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080)
