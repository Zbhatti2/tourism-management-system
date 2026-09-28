---
title: Manage Users Overview
order: 1
keywords: [users, teammates, add user, deactivate, reset password, tenant admin, role, temporary password, recovery phrase]
---
Manage Users is where a **Tenant Admin** looks after the people in their own
organization. Nobody here can see or touch another tenant's users.

## What you can do

- **Add a user** — pick a User ID (unique across the whole system), a role
  and a temporary password. Give them the password directly; they must
  choose their own the first time they log in.
- **Edit** — display name, email and role (User or Tenant Admin). You
  can't change your own role.
- **Reset password** — sets a new temporary password and a new recovery
  phrase for someone who is locked out.
- **Deactivate / reactivate** — blocks or restores someone's login while
  keeping everything they did on record.
- **Delete** — removes the login entirely. Their past actions stay in the
  audit log, just without a name attached.

## Recovery phrase

Every new or reset user gets a 12-word recovery phrase, shown **once**. With
it they can reset their own password from "Forgot your password?" on the
login page. Pass it to them with their temporary password.

## Safety rules

Your organization always keeps at least one active Tenant Admin — the last
one can't be demoted, deactivated or deleted.
