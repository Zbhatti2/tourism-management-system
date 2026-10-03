---
title: Point of Interest images
order: 2
keywords: [poi, point of interest, images, photos, master image catalog, platform, inherit, updates, review, auto-update]
---
Every Point of Interest has an **Images Catalog** too. It works the same
way as the Hotel and Restaurant albums: a slider and a list, search, and
curation before anything is added.

On a Point of Interest's page:

- **View Images** opens the album to browse only.
- **Images Catalog** opens it to edit, reorder and delete.
- **Add images** adds one or more photos, or a Zip of them.

To add photos for many Points of Interest at once, go to **Data Exchange
→ Import Point of Interest Images (Zip)**. Each file name must start
with the exact Point of Interest name, then a hyphen. For example,
`Badshahi Mosque-Courtyard at dusk.jpg`.

## Images from the platform

TMS keeps a **Master Image Catalog** for the Points of Interest in its
shared catalog. A Point of Interest you took from the platform catalog,
or linked to it, **inherits** those images. They're marked *Platform* in
your album.

Inherited images are yours to curate:

- **Change** an image's Title, Description, Date or Sort order.
- **Delete** an image. A deleted inherited image is never added back
  automatically.
- **Add** your own images next to the inherited ones.

## When the platform's images change

When TMS adds, re-captions or removes a master image, your setting
decides what happens. Tenant Admins choose it in **System Management →
Platform POI images**:

- **Update automatically**: the change is made straight away. A caption
  you edited yourself is kept.
- **Review before updating** (the default): each change waits on the
  **Platform image updates** page. **Accept** or **Skip** it there. A
  skipped change isn't offered again unless TMS changes that image
  again.
- **No updates**: your images stay as they are after the first
  inheritance.

The **Image updates** button on the Points of Interest list shows how
many changes are waiting. A Point of Interest's page shows the same for
that place.

A Point of Interest newly added from, or linked to, the platform catalog
always takes all of its master images, whatever the setting.
