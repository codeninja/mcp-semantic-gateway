---
name: manage-pet-lifecycle
description: |
  This skill allows you to manage a pet's lifecycle by adding a new pet, updating its details, and removing it from the store as needed.
allowed-tools:
  - addPet
  - updatePet
  - deletePet
---

## When to use this skill
Use this skill when you need to manage the lifecycle of a pet in the pet store, including adding new pets, updating their information, and deleting them when they are no longer needed.

## Procedure
1. **Add a New Pet**: Use the `addPet` tool to add a new pet to the store. Provide the required parameters: `id` and `name`.
2. **Update Pet Details**: If you need to update the pet's information, invoke the `updatePet` tool with the pet's `id` and the new `name`.
3. **Remove the Pet**: When the pet needs to be removed from the store, call the `deletePet` tool with the `petId` of the pet you wish to delete.
