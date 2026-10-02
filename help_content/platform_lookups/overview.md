---
title: Platform Lookups Overview
order: 1
keywords: [platform, lookups, locked, system admin, supplier types, poi types, hotel, resort, restaurant, star rating]
---
Platform Lookups is a **TMS System Admin** screen. It holds the lookup codes
that every tenant shares, so platform data always has a matching entry to
land in when a tenant adopts it:

- **Supplier Types**: Hotel, Resort, Restaurant
- **Supplier Sub-Types**: the star ratings (Five Star, 4-Star) under Hotel and Resort
- **POI Types**: the sightseeing types the platform Points of Interest catalog uses

## Locked in every tenant

Each entry here appears in every tenant's **Table Maintenance** with a
**Platform** lock badge. Tenants can use it, and can add their own entries
alongside it, but they can't edit, deactivate, merge or delete it.

## Making changes

- **New**: adds the entry to every tenant. If a tenant already has an entry
  with the same code or label, that entry is locked in place, so its
  existing suppliers or points of interest keep their links.
- **Edit**: a new label renames the entry in every tenant. The code can't
  be changed.
- **Remove** (unlock icon): takes the entry off the platform list. Nothing
  is deleted; each tenant keeps its copy as an ordinary, editable entry.
- **Re-apply to all tenants**: re-checks every tenant against this list.
  This normally happens automatically.

New tenants created from Tenant Management get the locked entries
automatically.

## Transport Hubs tab

This tab holds the **Hub Types** (Airport, Railway Station, Bus
Terminal, Seaport / Ferry Terminal). Transport Hubs is one shared list
that every tenant reads, so nothing is copied into tenants and these
aren't locked codes, just the options for a hub's Type. Add a type at
the bottom of the tab, or use the pencil to rename, re-order or
deactivate one. The number in the Hubs column opens the list of hubs of
that type.
