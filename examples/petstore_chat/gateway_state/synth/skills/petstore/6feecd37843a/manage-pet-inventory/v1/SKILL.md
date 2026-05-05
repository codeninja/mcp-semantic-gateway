---
name: manage-pet-inventory
description: |
  This skill allows you to manage the pet inventory by listing, adding, and removing pets from the store. It also enables updating the status of pets by removing sold ones from the inventory.
allowed-tools:
  - listPets
  - createPet
  - deletePet
---

## When to use this skill
Use this skill when you need to manage the pet inventory in a store, including viewing all available pets, adding new pets, and removing pets that are sold or no longer available.

## Procedure
1. **List all pets**: Use the `listPets` tool to retrieve all pets currently in the store.
2. **Add a new pet**: If you want to add a new pet, use the `createPet` tool with the required parameters: `id`, `name`, and `species`.
3. **Remove sold pets**: After listing the pets, identify any that have been sold and use the `deletePet` tool to remove them from the inventory.
4. **Remove other pets**: If there are pets that are no longer available for sale, use the `deletePet` tool again to remove those pets as needed.
