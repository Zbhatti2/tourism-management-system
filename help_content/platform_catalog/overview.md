---
title: Platform Catalogs Overview
order: 1
keywords: [points of interest, accommodation, reference room rate, hotels, restaurants, embassies, consulates, catalog, duplicates, merge, undo, alternate names]
---
**Points of Interest**, **Accommodation**, **Restaurants** and
**Embassies & Consulates** are the platform catalogs: shared data that
TMS maintains for every tenant. Only the SystemAdmin can see or change
them here.

An Accommodation record can carry a **Reference Room Rate** (USD, double
room per night) and its *as of* date. It's a guide for the Tour Planner
only. Tenants receive it through Catalog Sync and can override it.

## The list

Search by name, other spellings or notes, and filter by type, country
and city. The list shows up to 500 records at a time. Each record has
**Alternate Names** (other spellings or former names, one per line):
imports that use one of them match the record.

## Find duplicates

**Find duplicates** lists pairs that are probably the same place:

- in the same city with the same name once tidied, names that sound
  alike, or very similar names (the city's own name is left out of the
  comparison);
- or the same type of place within 150 m of each other.

Open a pair with **Compare & merge**, or leave it if they really are
different places.

## Merge

Choose which record to **keep** (the buttons at the top), then, for
each field where they differ, whose value survives. An empty field is
always filled from the other record. Notes from both are kept. The
merged-away name becomes an alternate name, and anything linked to it
moves to the kept record (for Transport Hubs: package route stops,
airline tickets and knowledge-graph links).

## Merge log and Undo

Every merge is listed in the **Merge log**. **Undo** puts both records
and every moved link back exactly as they were. If a later merge
involves the same records, undo that one first.

Transport Hubs use the same Find duplicates, Merge and Merge log
screens; the buttons are on the Transport Hubs list.

## Notes / History and Additional links (Points of Interest)

A POI's **Notes / History** holds its history, significance and visiting
notes. Imports and the PDF agent add to it, never replace it.

**Additional links** replaces the old Image URL and Video URL fields. Their
values were moved here. Add as many links as you like, each with a **Type**
(Video, Images, Document or Other) and an optional title. Tenants' copies
of a POI receive its links when the catalog syncs. A tenant's own links are
never removed.

