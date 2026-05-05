---
name: manage-petstore-inventory
description: |
  This skill allows you to manage and monitor the pet store inventory by listing available pets, adding new pets, fetching details of specific pets, and removing pets that are no longer available.
allowed-tools:
  - listPets
  - createPet
  - getPet
  - deletePet
---

## When to use this skill
Use this skill when you need to manage the pet store inventory, including adding new pets, viewing all available pets, fetching details of specific pets, and removing pets that are sold or no longer available.

## Procedure
1. **List all pets**: Use the `listPets` tool to view all available pets in the store.
2. **Add a new pet**: If you want to add a new pet, use the `createPet` tool with the required parameters: `id`, `name`, and `species`.
3. **Fetch details of a specific pet**: To get more information about a specific pet, use the `getPet` tool with the `pet_id` of the pet you want to inquire about.
4. **Remove pets**: If a pet is sold or no longer available, use the `deletePet` tool with the `pet_id` of the pet you wish to remove from the inventory.
