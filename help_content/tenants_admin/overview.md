---
title: Tenant Management Overview
order: 1
keywords: [tenants, system admin, new tenant, suspend, reactivate, account number, platform]
---
Tenant Management is the **TMS System Admin** console. It is only visible
to the SystemAdmin role, which belongs to no tenant.

## What you can do

- **New Tenant** — creates a new tour operator with its own isolated data,
  its own encryption key, the next 8-digit account number, a copy of the
  standard lookup tables, and its first Tenant Admin login. That admin
  then adds their own teammates from Manage Users.
- **Suspend / Reactivate** — a suspended tenant's users can't log in; its
  data is untouched.

## Account numbers

Numbers are assigned once and never reused. 10000001 is reserved for the
TMS Platform itself; Ma Vie Tours is 10000002.

## Also for the System Admin

Database health and Backup & Restore (under System Management) cover the
whole database — every tenant — so they are SystemAdmin-only.
