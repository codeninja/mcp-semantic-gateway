---
name: retrieve-and-update-pet
description: |
  This skill retrieves detailed information about a specific pet by its ID and allows for updating its name or status if necessary.
allowed-tools:
  - getPetById
  - updatePetWithForm
---

## When to use this skill
Use this skill when you need to get detailed information about a pet using its ID and potentially update its name or status based on the retrieved information.

## Procedure
1. Call the `getPetById` tool with the specific `petId` to retrieve the pet's details.
2. Review the retrieved information to determine if an update is needed for the pet's name or status.
3. If an update is necessary, call the `updatePetWithForm` tool with the `petId` and the new name or status to apply the changes.
