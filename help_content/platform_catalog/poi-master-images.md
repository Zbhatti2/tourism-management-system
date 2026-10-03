---
title: POI Master Image Catalog
order: 2
keywords: [master image catalog, poi images, platform images, licence, wikimedia, image collector, tms agent, inherit]
---
Each platform Point of Interest has a **Master Image Catalog**. Tenants
inherit it: their Points of Interest that came from the platform catalog,
or are linked to it, receive these images.

## Adding images

- On a POI's page, **Master Image Catalog → Add images** adds photos, or
  a Zip of them, for that POI.
- **Points of Interest → Import Images** takes a Zip for many POIs at
  once. Each file name must start with the exact POI name, then a
  hyphen. **Download the exact names** gives you the list.
- The **POI Image Collector**, a TMS Agent, finds photos on the web. It
  looks at official sites, tourism boards and Wikimedia Commons. Start it
  with **Find photos with AI** on a POI's catalog, or from the Import
  Images page or the TMS Agents page for several POIs at once. Runs are a
  platform cost: the *Platform agents* line on AI Usage, using
  `PLATFORM_ANTHROPIC_API_KEY` when it's set.

Every image is curated before it's added, as for tenant albums. Platform
images also have a **Licence / credit** field. The collector fills it in
when the page states one. **Check each licence before you approve a
photo.**

## Editing and removing

To remove one image, use **Delete** under it in the **Album** view.
In the **List** view, edit Title, Description, Licence, Date and Sort
order, or delete images.

- **Changing** a Title, Description, Date or Licence counts as an update
  for tenants.
- **Deleting** an image retires it from the master catalog. Tenants that
  have it keep it or lose it according to their setting.

Each tenant chooses how updates reach them: **Update automatically**,
**Review before updating** (the default) or **No updates**.
