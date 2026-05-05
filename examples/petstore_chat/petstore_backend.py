"""Tiny petstore HTTP backend used by the chat demo.

FastAPI auto-generates an OpenAPI 3 spec at ``/openapi.json``; the gateway
ingests that spec, the chat CLI then exposes the resulting tools to a
Claude agent.

State is in-memory and seeded with three pets so the agent has something
to find on first turn.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(
    title="Petstore Demo",
    version="1.0.0",
    description="Tiny in-memory petstore for the gateway chat demo.",
)


class Pet(BaseModel):
    id: str = Field(..., description="Stable identifier for the pet.")
    name: str = Field(..., description="Display name.")
    species: str = Field(..., description='e.g. "dog", "cat", "fish".')
    status: str = Field(
        default="available",
        description='Inventory status: "available", "pending", or "sold".',
    )


_pets: dict[str, Pet] = {
    "1": Pet(id="1", name="Fido", species="dog", status="available"),
    "2": Pet(id="2", name="Whiskers", species="cat", status="available"),
    "3": Pet(id="3", name="Mr. Bubbles", species="fish", status="sold"),
    "4": Pet(id="4", name="Rex", species="dog", status="pending"),
}


@app.get(
    "/pets",
    operation_id="listPets",
    summary="List pets, optionally filtered by status.",
    response_model=List[Pet],
)
def list_pets(status: Optional[str] = None) -> List[Pet]:
    """Return all pets in the store. If ``status`` is provided, only pets
    with that status are returned (``available`` / ``pending`` / ``sold``)."""

    if status is None:
        return list(_pets.values())
    return [p for p in _pets.values() if p.status == status]


@app.get(
    "/pets/{pet_id}",
    operation_id="getPet",
    summary="Fetch a single pet by ID.",
    response_model=Pet,
)
def get_pet(pet_id: str) -> Pet:
    if pet_id not in _pets:
        raise HTTPException(status_code=404, detail=f"Pet {pet_id!r} not found")
    return _pets[pet_id]


@app.post(
    "/pets",
    operation_id="createPet",
    summary="Add a new pet to the store.",
    response_model=Pet,
    status_code=201,
)
def create_pet(pet: Pet) -> Pet:
    if pet.id in _pets:
        raise HTTPException(status_code=409, detail=f"Pet {pet.id!r} already exists")
    _pets[pet.id] = pet
    return pet


@app.delete(
    "/pets/{pet_id}",
    operation_id="deletePet",
    summary="Remove a pet from the store.",
)
def delete_pet(pet_id: str) -> dict:
    if pet_id not in _pets:
        raise HTTPException(status_code=404, detail=f"Pet {pet_id!r} not found")
    del _pets[pet_id]
    return {"deleted": pet_id}
