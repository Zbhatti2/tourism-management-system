---
title: Catalog Sync Overview
order: 1
keywords: [catalog, sync, tenant, adopt, points of interest, hotels, accommodation, restaurants, suppliers, link, platform updates]
---
Catalog Sync is a **TMS System Admin** screen. It copies the platform's
**Points of Interest**, **Accommodation** and **Restaurants** into each
tenant's own Points of Interest and Suppliers, and then keeps those
copies up to date.

## A tenant's first sync

Open **Catalog Sync**, pick the tenant and click **Preview first sync**.
Nothing changes until you click **Sync**. Each catalog record is shown as
one of:

- **Add**: the tenant has nothing like it, so a copy is created.
  Hotels and resorts become Suppliers under the locked **Hotel** or
  **Resort** type, and restaurants under **Restaurant**. 4- and 5-star
  properties get the matching sub-type. They get no Primary or
  Secondary preference.
- **Link**: the tenant already has a record with the same name in the
  same city, so the two are tied together instead of making a second
  copy. The tenant's own values are kept; only its empty fields are
  filled from the catalog.
- **Review**: a possible match, such as a name that sounds alike
  (Tamboo / Tambu). Choose **Link**, **Add as new** or **Skip**.

Any record under *to add* can be ticked to skip it. A skipped record
isn't offered again.

## After the first sync

Platform changes now reach the tenant on their own:

- **Edits** in the catalog, imports and merges update the tenant's copy
  of every field the tenant hasn't changed.
- **New catalog records** are added. If the tenant already has the same
  name in the same city, the new record is linked to it instead. A
  possible match waits on this screen as *to review*.
- **Deleted catalog records** leave the tenant's copy in place. It just
  stops syncing.

New tenants get the whole catalog when they're created, and stay in
sync from then on.

## What is copied

| Points of Interest | Suppliers |
|---|---|
| Name, POI Type, Year Established, location, address (Local Location), phone, website, description (Historical Significance), coordinates | Name, type and star sub-type, website, address, phone, email, and for hotels the amenities and the **Reference Room Rate** |

Everything else in the catalog, such as entry fee, opening hours, star
rating, number of rooms, cuisine, price range, images and catalog notes, is
shown live on the tenant's record under **From the Platform Catalog**.
The tenant's own notes are never touched.

### Reference Room Rate

A hotel in the platform Accommodation catalog can carry a **Reference Room
Rate**: a typical double room per night in US dollars, with its *as of*
date. It reaches the tenant's Supplier like any other synced field, and the
tenant can change it on the Supplier's Edit screen. A rate the tenant has
changed is kept: a later platform change waits on this screen as an update
to review. The rate is used only by the **Tour Planner**, to estimate
lodging. Contracted prices stay in the Supplier's room types.
