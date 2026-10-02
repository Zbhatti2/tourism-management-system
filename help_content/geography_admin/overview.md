---
title: Geography & Distances Overview
order: 1
keywords: [geography, regions, countries, states, provinces, cities, shared, global]
---
Geography & Distances is a **TMS System Admin** screen (in the Platform
Admin sidebar). It manages the four
geography tables that power the Region → Country → Province/State → City
pickers used everywhere in TMS: Regions, Countries, States/Provinces, and
Cities.

## Shared across every tenant

These four tables have no tenant scoping at all — a relabel, merge, or
delete made here is visible to, and affects, every tenant. That is why
only the SystemAdmin can edit them; tenants use them through the pickers
on their own forms.

## Hierarchy and search

Each level (except Regions, at the top) requires picking its parent —
a Province/State must belong to a Country, a City to a Province/State,
and so on. Because Countries alone has over 200 rows, this screen has a
free-text search box, and, wherever a table has a parent, a dropdown to
filter down to just that parent's children (for example, only Pakistan's
provinces).

## City coordinates and time zones

Each city can hold its centre point (paste coordinates from Google Maps)
and its time zone (for example Asia/Karachi). They are used for maps,
straight-line distances and finding the nearest airport or station. The
Cities list flags any city still **Missing** coordinates. Most were
filled in automatically from GeoNames, a free open geographic database.

## Distances between Cities

**Distances between Cities** holds one entry per pair of cities (it covers
both directions): road distance, typical drive time, whether there is a
rail link, where the figures came from and when they were checked. The
straight-line distance is always worked out from the two cities'
coordinates, so it never needs entering. Filter the list by city to see
every distance on file from that city.

