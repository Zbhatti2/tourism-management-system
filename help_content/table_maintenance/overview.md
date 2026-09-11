---
title: Table Maintenance Overview
order: 1
keywords: [table maintenance, lookup tables, dropdowns, code, label, sort order, active, parent]
---
Table Maintenance (under **Tables & Utilities**) is where the lookup
tables behind dropdowns across the app are managed — Supplier Types,
Document Types, POI Types, Amenity Options, Room Types, Job Roles, and
many more, all grouped by area on this one page.

## Editing a lookup table

Every lookup table shares the same basic shape: a **code**, a **label**
(what shows in the dropdown), an optional **description**, a **sort
order**, and an **active** flag — an inactive entry stops appearing in
new dropdowns but existing records that already reference it are
unaffected.

## Nested lookups

Some tables are nested under another — Hotel Amenity Options and Hotel
Room Types, for example, only apply when a Supplier's type is "Hotel."
Those show up grouped under their parent in the Table Maintenance list
rather than as an independent flat list.

## Table-specific extra fields

A few tables carry fields beyond the standard code/label/description
shape — Amenity Options, for instance, also has a **category** and an
**icon**. Those extra fields appear automatically on the Add/Edit form
for the tables that use them; the rest of the tables just show the
standard fields.
