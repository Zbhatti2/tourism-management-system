---
title: Points of Interest Overview
order: 1
keywords: [poi, points of interest, attraction, images, photographs, links, map coordinates, knowledge graph]
---
Points of Interest (POIs) are the attractions a tour visits — gurdwaras,
mosques, museums, archaeological sites, and anything else a tour
operator maintains for itinerary planning. Reach the module from the
Dashboard's Points of Interest tile.

## What's on a POI record

- **Core details** — name, type (from a Table Maintenance lookup), and
  year established.
- **Location** — the same Region → Country → Province/State → City
  cascade used elsewhere in TMS, plus a local-location free-text field,
  directions, and map coordinates (typed or pasted in almost any format —
  decimal degrees or DMS — and normalized to DMS with a Google Maps link
  on the view page).
- **Contact** — phone, fax, website, a linked local Contact, and a
  governing-authority Organization.
- **Background** — historical significance and general notes.
- **Links** — a structured list of Link #1 / Link #2 / ... rows, each
  with its own URL and a one-line description. Click **Add link** on the
  Edit form to add a row, or the "×" next to a row to remove it; blank
  rows are dropped automatically when you save.
- **Images / Photographs** — click the **Images/Photographs** button next
  to Year established (only available once the POI has been saved at
  least once) to open its own page: add an image via a Cloud Link or a
  Local Drive Path (with a **Browse…** picker), or use **Bulk Import** to
  add several at once with one native multi-select file dialog.
- **Knowledge graph data** — a freeform, semicolon-separated field for
  loosely structured relationships (e.g. "Managed by the Auqaf
  Department"), plus a **structured** Knowledge Graph section built from
  formal edges to other records.

## Searching and filtering

The list page filters by Type, City, Province/State, and Country, plus a
name search.
