---
name: fetch-pet-details
description: |
  This skill allows an agent to fetch details of a specific pet using its ID, aiding in decisions regarding adoption or purchase.
allowed-tools:
  - getPet
---

## When to use this skill
Use this skill when you need to retrieve detailed information about a specific pet from the pet store, which can assist in making informed decisions about adoption or purchase.

## Procedure
1. Identify the pet ID of the pet you want to inquire about.
2. Use the `getPet` tool with the identified pet ID as a parameter.
3. Review the returned details to understand the pet's characteristics, status, and other relevant information.
4. Make decisions based on the fetched details regarding adoption or purchase.
