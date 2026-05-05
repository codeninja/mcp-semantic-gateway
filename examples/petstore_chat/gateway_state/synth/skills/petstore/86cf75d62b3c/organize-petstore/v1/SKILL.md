---
name: organize-petstore
description: |
  This skill helps organize and maintain the pet store by finding pets by their status, updating their details, and uploading new photos for them.
allowed-tools:
  - findPetsByStatus
  - updatePet
  - uploadFile
---

## When to use this skill
Use this skill when you need to manage pets in the pet store by finding them based on their status, updating their information, and attaching new photos.

## Procedure
1. Call `findPetsByStatus` to retrieve pets based on their current status (available, pending, or sold).
2. For each pet retrieved, use `updatePet` to modify their details as necessary, providing the pet's ID and the updated name.
3. If you have a new photo for any pet, use `uploadFile` with the pet's ID and the URL of the photo to attach it to the pet's profile.
