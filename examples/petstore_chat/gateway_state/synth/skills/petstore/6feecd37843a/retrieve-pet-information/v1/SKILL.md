---
name: retrieve-pet-information
description: |
  This skill allows agents to retrieve detailed information about a specific pet using its ID, facilitating targeted inquiries about individual animals in the store.
allowed-tools:
  - getPet
---

## When to use this skill
Use this skill when you need to obtain detailed information about a specific pet in the store by its unique ID.

## Procedure
1. Identify the pet ID for the pet you want to inquire about.
2. Use the `getPet` tool with the identified pet ID to fetch the pet's details.
3. Review the returned information to understand the pet's characteristics and status.
