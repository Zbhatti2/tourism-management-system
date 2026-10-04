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

## Duplicates

Two spellings of the same place, such as "Khyber Pakhtunkhwa" and "Kyber
Pakhtun Khawa", make it hard to know which one to pick when adding cities
and addresses.

- **Possible duplicates.** The Provinces / States and Cities lists show a
  **Possible duplicates** panel at the top. It lists names that are the
  same, sound alike or are near spellings, under the same country or
  province, with how much each one is used.
- **Merging.** **Merge…** moves everything that uses the first entry to the
  second, then deletes the first. That covers addresses of every kind,
  Points of Interest, platform hotels and restaurants, packages, transport
  hubs and embassies, for every tenant.
  - The old name is kept as another spelling of the entry you keep, so
    imports that use it still find it.
  - Merging two provinces also moves their cities. A city that is listed
    under both, like Peshawar, becomes one city, keeping its distances and
    coordinates.
  - **the other way** keeps the first entry instead.
- **Adding a new entry.** If the name looks like one already in the table,
  you are asked to confirm before it is added.
- **Imports.** Imports now treat "Khyber Pakhtun Khwa" and "Khyber
  Pakhtunkhwa" as the same name, because spaces don't count.

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

