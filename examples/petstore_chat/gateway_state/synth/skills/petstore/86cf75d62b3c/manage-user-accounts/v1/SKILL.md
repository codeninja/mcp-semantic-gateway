---
name: manage-user-accounts
description: |
  This skill allows for the management of user accounts in the pet store system. It includes creating new users, updating user profiles, and deleting users as needed.
allowed-tools:
  - createUser
  - createUsersWithListInput
  - updateUser
  - deleteUser
---

## When to use this skill
Use this skill when you need to manage user accounts in the pet store system, including creating, updating, or deleting user profiles.

## Procedure
1. **Create a New User**: Use `createUser` to create a single user by providing the required parameters: `id` and `username`.  
2. **Create Multiple Users**: If you need to create multiple users at once, utilize `createUsersWithListInput` with the appropriate `requestBody` containing user details.  
3. **Update User Profile**: To update an existing user's profile, call `updateUser` with the user's `username` and `id`.  
4. **Delete a User**: If a user needs to be removed, use `deleteUser` by providing the `username` of the user to be deleted.
