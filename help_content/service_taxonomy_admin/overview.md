---
title: Service Code Maintenance Overview
order: 1
keywords: [service code, taxonomy, category, group, sub-group, shared, global]
---
Service Code Maintenance (under **Tables & Utilities**) manages the
three-level Service Coding System taxonomy that every Service is
classified against: Categories → Groups → Sub-Groups.

## Shared across every tenant

Like Geography Maintenance, these three tables have no tenant scoping —
an edit here affects every tenant, not just yours.

## Codes are permanent, names aren't

A Category/Group/Sub-Group **code** is immutable once created, since it's
already baked into every Service Code string issued against it (for
example `TP-MN-MC-0001`). Renaming out from under existing services would
leave their stored codes stale. The **name** and **description**,
though, can always be edited safely and take effect everywhere
instantly.

## Deleting

A row with children underneath it (a Category with Groups, a Group with
Sub-Groups, a Sub-Group with Services already issued against it) can't be
deleted outright — deactivate it instead. There's no merge/reassign flow
here the way Geography Maintenance has, since merging two different
service classifications isn't a safe automatic operation.
