---
name: manage-petstore-inventory
description: |
  This skill allows an agent to manage the pet store inventory by listing all pets, adding new pets, and removing pets as needed.
allowed-tools:
  - listPets
  - createPet
  - deletePet
---

## When to use this skill
Use this skill when you need to manage the inventory of a pet store, including listing existing pets, adding new pets, or removing pets that are no longer available.

## Procedure
1. **List all pets**: Start by invoking the `listPets` tool to retrieve the current inventory of pets in the store.
2. **Add a new pet**: If you need to add a new pet, gather the required information (id, name, species) and use the `createPet` tool to add the new pet to the inventory.
3. **Remove a pet**: If a pet needs to be removed, identify the pet by its ID and use the `deletePet` tool to remove it from the store's inventory.
