---
name: retrieve-user-information
description: |
  This skill retrieves user information by fetching details using the `getUserByName` tool, enabling user-specific actions or updates based on their profile.
allowed-tools:
  - getUserByName
---

## When to use this skill
Use this skill when you need to obtain detailed information about a specific user in the pet store system. This is essential for performing user-specific actions or updates based on their profile.

## Procedure
1. Identify the username of the user whose information you want to retrieve.
2. Call the `getUserByName` tool with the identified username as a parameter.
3. Process the returned user details for any necessary actions or updates.
4. Utilize the retrieved information as needed for further operations or user interactions.
