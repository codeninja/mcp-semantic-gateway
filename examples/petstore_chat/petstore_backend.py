"""Petstore HTTP backend used by the chat demo.

Models the canonical Swagger Petstore surface area (pet / store / user)
so the gateway has three resource domains to mine, cluster, and
synthesize skills from. Implementation is deliberately simple: in-memory
dicts, JSON only, no auth, no XML, no real file uploads.

FastAPI auto-generates an OpenAPI 3 spec at ``/openapi.json``; the
gateway ingests that spec and the chat CLI exposes the resulting tools
to a Claude/OpenAI agent.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(
    title="Petstore Demo",
    version="1.0.0",
    description=(
        "In-memory Swagger-style petstore for the gateway chat demo. "
        "Three resource groups (pet, store, user) so skill synthesis has "
        "real clusters to discover."
    ),
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class Category(BaseModel):
    id: int
    name: str


class Tag(BaseModel):
    id: int
    name: str


class Pet(BaseModel):
    id: int = Field(..., description="Stable identifier for the pet.")
    name: str = Field(..., description="Display name.")
    category: Optional[Category] = None
    photoUrls: List[str] = Field(default_factory=list)
    tags: List[Tag] = Field(default_factory=list)
    status: str = Field(
        default="available",
        description='Inventory status: "available", "pending", or "sold".',
    )


class Order(BaseModel):
    id: int
    petId: int
    quantity: int = 1
    shipDate: Optional[str] = None
    status: str = Field(default="placed", description='"placed", "approved", or "delivered".')
    complete: bool = False


class User(BaseModel):
    id: int
    username: str
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None
    phone: Optional[str] = None
    userStatus: int = 0


class ApiResponse(BaseModel):
    code: int
    type: str
    message: str


# ---------------------------------------------------------------------------
# In-memory state (seeded so the agent has something on first turn)
# ---------------------------------------------------------------------------


_DOGS = Category(id=1, name="Dogs")
_CATS = Category(id=2, name="Cats")
_FISH = Category(id=3, name="Fish")

_pets: dict[int, Pet] = {
    1: Pet(id=1, name="Fido", category=_DOGS, status="available",
           tags=[Tag(id=1, name="friendly")], photoUrls=["https://example.com/fido.jpg"]),
    2: Pet(id=2, name="Whiskers", category=_CATS, status="available",
           tags=[Tag(id=2, name="quiet")]),
    3: Pet(id=3, name="Mr. Bubbles", category=_FISH, status="sold"),
    4: Pet(id=4, name="Rex", category=_DOGS, status="pending",
           tags=[Tag(id=1, name="friendly"), Tag(id=3, name="trained")]),
}

_orders: dict[int, Order] = {
    1001: Order(id=1001, petId=3, quantity=1, status="delivered", complete=True),
    1002: Order(id=1002, petId=4, quantity=1, status="placed", complete=False),
}

_users: dict[str, User] = {
    "alice": User(id=1, username="alice", firstName="Alice", lastName="Smith",
                  email="alice@example.com", userStatus=1),
    "bob": User(id=2, username="bob", firstName="Bob", lastName="Jones",
                email="bob@example.com", userStatus=1),
}

# Username → True when the session is "logged in". Trivial state used so
# loginUser/logoutUser have observable effects.
_active_sessions: set[str] = set()


# ---------------------------------------------------------------------------
# /pet
# ---------------------------------------------------------------------------


@app.post("/pet", operation_id="addPet", summary="Add a new pet to the store.",
          response_model=Pet, status_code=200)
def add_pet(pet: Pet) -> Pet:
    if pet.id in _pets:
        raise HTTPException(status_code=409, detail=f"Pet {pet.id} already exists")
    _pets[pet.id] = pet
    return pet


@app.put("/pet", operation_id="updatePet", summary="Update an existing pet.",
         response_model=Pet)
def update_pet(pet: Pet) -> Pet:
    if pet.id not in _pets:
        raise HTTPException(status_code=404, detail=f"Pet {pet.id} not found")
    _pets[pet.id] = pet
    return pet


@app.get("/pet/findByStatus", operation_id="findPetsByStatus",
         summary="Find pets by status.", response_model=List[Pet])
def find_pets_by_status(status: str = "available") -> List[Pet]:
    """Return pets matching ``status`` (``available`` / ``pending`` / ``sold``)."""

    return [p for p in _pets.values() if p.status == status]


@app.get("/pet/findByTags", operation_id="findPetsByTags",
         summary="Find pets by tags.", response_model=List[Pet])
def find_pets_by_tags(tags: List[str]) -> List[Pet]:
    """Return pets that carry any of the supplied tag names."""

    wanted = set(tags)
    return [p for p in _pets.values() if any(t.name in wanted for t in p.tags)]


@app.get("/pet/{petId}", operation_id="getPetById",
         summary="Find pet by ID.", response_model=Pet)
def get_pet_by_id(petId: int) -> Pet:
    if petId not in _pets:
        raise HTTPException(status_code=404, detail=f"Pet {petId} not found")
    return _pets[petId]


@app.post("/pet/{petId}", operation_id="updatePetWithForm",
          summary="Update a pet's name or status by ID.", response_model=Pet)
def update_pet_with_form(
    petId: int, name: Optional[str] = None, status: Optional[str] = None
) -> Pet:
    if petId not in _pets:
        raise HTTPException(status_code=404, detail=f"Pet {petId} not found")
    pet = _pets[petId]
    if name is not None:
        pet.name = name
    if status is not None:
        pet.status = status
    _pets[petId] = pet
    return pet


@app.delete("/pet/{petId}", operation_id="deletePet",
            summary="Remove a pet from the store.", response_model=ApiResponse)
def delete_pet(petId: int) -> ApiResponse:
    if petId not in _pets:
        raise HTTPException(status_code=404, detail=f"Pet {petId} not found")
    del _pets[petId]
    return ApiResponse(code=200, type="ok", message=f"deleted pet {petId}")


class _UploadImageBody(BaseModel):
    """JSON stand-in for the spec's binary upload — the demo doesn't need
    real bytes, just an observable effect."""

    additionalMetadata: Optional[str] = None
    photoUrl: str = Field(..., description="URL to record as the pet's photo.")


@app.post("/pet/{petId}/uploadImage", operation_id="uploadFile",
          summary="Attach a photo URL to a pet.", response_model=ApiResponse)
def upload_file(petId: int, body: _UploadImageBody) -> ApiResponse:
    if petId not in _pets:
        raise HTTPException(status_code=404, detail=f"Pet {petId} not found")
    _pets[petId].photoUrls.append(body.photoUrl)
    msg = f"attached {body.photoUrl} to pet {petId}"
    if body.additionalMetadata:
        msg += f" ({body.additionalMetadata})"
    return ApiResponse(code=200, type="ok", message=msg)


# ---------------------------------------------------------------------------
# /store
# ---------------------------------------------------------------------------


@app.get("/store/inventory", operation_id="getInventory",
         summary="Return pet counts grouped by status.")
def get_inventory() -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in _pets.values():
        counts[p.status] = counts.get(p.status, 0) + 1
    return counts


@app.post("/store/order", operation_id="placeOrder",
          summary="Place an order for a pet.", response_model=Order)
def place_order(order: Order) -> Order:
    if order.id in _orders:
        raise HTTPException(status_code=409, detail=f"Order {order.id} already exists")
    if order.petId not in _pets:
        raise HTTPException(status_code=400, detail=f"Pet {order.petId} not in catalog")
    _orders[order.id] = order
    return order


@app.get("/store/order/{orderId}", operation_id="getOrderById",
         summary="Look up a purchase order by ID.", response_model=Order)
def get_order_by_id(orderId: int) -> Order:
    if orderId not in _orders:
        raise HTTPException(status_code=404, detail=f"Order {orderId} not found")
    return _orders[orderId]


@app.delete("/store/order/{orderId}", operation_id="deleteOrder",
            summary="Cancel a purchase order.", response_model=ApiResponse)
def delete_order(orderId: int) -> ApiResponse:
    if orderId not in _orders:
        raise HTTPException(status_code=404, detail=f"Order {orderId} not found")
    del _orders[orderId]
    return ApiResponse(code=200, type="ok", message=f"deleted order {orderId}")


# ---------------------------------------------------------------------------
# /user
# ---------------------------------------------------------------------------


@app.post("/user", operation_id="createUser",
          summary="Create a single user.", response_model=User)
def create_user(user: User) -> User:
    if user.username in _users:
        raise HTTPException(status_code=409, detail=f"User {user.username!r} already exists")
    _users[user.username] = user
    return user


@app.post("/user/createWithList", operation_id="createUsersWithListInput",
          summary="Create multiple users in one call.", response_model=List[User])
def create_users_with_list(users: List[User]) -> List[User]:
    created: list[User] = []
    for u in users:
        if u.username in _users:
            continue
        _users[u.username] = u
        created.append(u)
    return created


@app.get("/user/login", operation_id="loginUser",
         summary="Log a user into the system.")
def login_user(username: str, password: Optional[str] = None) -> dict:
    if username not in _users:
        raise HTTPException(status_code=404, detail=f"User {username!r} not found")
    _active_sessions.add(username)
    return {"session": f"session-token-for-{username}", "active": sorted(_active_sessions)}


@app.get("/user/logout", operation_id="logoutUser",
         summary="Log the current users out of the system.")
def logout_user() -> dict:
    cleared = sorted(_active_sessions)
    _active_sessions.clear()
    return {"logged_out": cleared}


@app.get("/user/{username}", operation_id="getUserByName",
         summary="Get a user by username.", response_model=User)
def get_user_by_name(username: str) -> User:
    if username not in _users:
        raise HTTPException(status_code=404, detail=f"User {username!r} not found")
    return _users[username]


@app.put("/user/{username}", operation_id="updateUser",
         summary="Update a user's profile.", response_model=User)
def update_user(username: str, user: User) -> User:
    if username not in _users:
        raise HTTPException(status_code=404, detail=f"User {username!r} not found")
    # Allow renaming via the body; drop the old key so lookups stay consistent.
    if user.username != username:
        _users.pop(username, None)
    _users[user.username] = user
    return user


@app.delete("/user/{username}", operation_id="deleteUser",
            summary="Delete a user.", response_model=ApiResponse)
def delete_user(username: str) -> ApiResponse:
    if username not in _users:
        raise HTTPException(status_code=404, detail=f"User {username!r} not found")
    del _users[username]
    _active_sessions.discard(username)
    return ApiResponse(code=200, type="ok", message=f"deleted user {username!r}")
