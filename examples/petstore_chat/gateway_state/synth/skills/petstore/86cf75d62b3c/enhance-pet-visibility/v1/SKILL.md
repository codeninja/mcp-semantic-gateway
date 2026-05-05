---
name: enhance-pet-visibility
description: |
  This skill enhances the visibility of pets in the store by tagging them with specific labels and updating their information as needed.
allowed-tools:
  - findPetsByTags
  - updatePet
---

## When to use this skill
Use this skill when you want to improve the visibility of pets in the store by tagging them with specific labels and ensuring their information is up to date.

## Procedure
1. Use the `findPetsByTags` tool to retrieve a list of pets that carry the specified tags. Provide the necessary `requestBody` with the tags you want to search for.
2. Review the list of pets returned by `findPetsByTags`.
3. For each pet that requires an update, use the `updatePet` tool. Provide the `id` of the pet and the new `name` or other information you wish to update.
4. Confirm that the updates have been successfully applied to the pets' information.
