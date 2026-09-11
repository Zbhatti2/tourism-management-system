---
title: Geography Maintenance Overview
order: 1
keywords: [geography, regions, countries, states, provinces, cities, shared, global]
---
Geography Maintenance (under **Tables & Utilities**) manages the four
geography tables that power the Region → Country → Province/State → City
pickers used everywhere in TMS: Regions, Countries, States/Provinces, and
Cities.

## Shared across every tenant

Unlike most lookup tables in Table Maintenance, these four have no
tenant scoping at all — a relabel, merge, or delete made here is visible
to, and affects, every tenant, not just yours. Edit with that in mind.

## Hierarchy and search

Each level (except Regions, at the top) requires picking its parent —
a Province/State must belong to a Country, a City to a Province/State,
and so on. Because Countries alone has over 200 rows, this screen has a
free-text search box, and, wherever a table has a parent, a dropdown to
filter down to just that parent's children (for example, only Pakistan's
provinces).
