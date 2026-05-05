---
name: petstore-authentication
description: |
  This skill facilitates user authentication in the pet store system by logging users in and out, ensuring secure access to the platform.
allowed-tools:
  - loginUser
  - logoutUser
---

## When to use this skill
Use this skill when you need to authenticate users in the pet store system, allowing them to securely log in and log out.

## Procedure
1. To log a user in, call the `loginUser` tool with the required `username` parameter.
2. Confirm the user is logged in and has access to the system.
3. When the user needs to log out, invoke the `logoutUser` tool to securely log them out of the system, ensuring their session is terminated properly.
