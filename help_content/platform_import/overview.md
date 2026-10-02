---
title: Data Import Overview
order: 1
keywords: [import, excel, csv, template, duplicates, merge, notes only, cities, distances, hubs, points of interest, hotels, accommodation, restaurants, embassies]
---
Data Import is a **TMS System Admin** screen for loading platform data
from Excel or CSV, one kind of record at a time:

1. Cities
2. City Distances
3. Transport Hubs
4. Points of Interest
5. Accommodation
6. Restaurants
7. Embassies & Consulates

Load **Cities** first, because every other template refers to cities by
name.

## Templates

Pick an entity and click **Download blank template**. Each template has
a *Read Me* sheet, the data sheet (required columns have a dark red
header) and a *Lists* sheet behind the drop-downs. "N/V", "N/A" and
"verify" are read as blank, so nothing is ever guessed.

## Checking a file

**Check file** reads the file but saves nothing. Each row is labelled:

- **New**: nothing like it in TMS; it will be added.
- **Update**: it matches a record already in TMS. Only that record's
  *empty* fields are filled, so existing values are never overwritten.
- **Possible duplicate**: a record (or an earlier row in the same file)
  has the same name once tidied, a name that *sounds alike* (Ahmad /
  Ahmed, Masjid / Mosque, Qila / Kila), a very similar name, or is the
  same type of place within 150 m. Choose **Merge into it**, **Keep as
  new** or **Skip**. Exact and sounds-alike matches default to Merge;
  look-alikes such as Baltit Fort / Altit Fort default to Keep as new.
- **Problem**: the row can't be imported, and the reason is shown (a
  missing required value, a city TMS doesn't know, bad coordinates).

**Add missing provinces and cities** lets the import add places TMS
doesn't know yet. A city abroad with no province goes under
"(Province not set)" for its country, which you can tidy later in
Geography & Distances. Tick or untick it and click **Check again**.

**Import** then writes every row except Problems. The file stays in
*Recent files* with the result of each row.

## Notes

Notes are always **added**, never replaced. Each addition gets a heading
with the date and source, so a record's notes build into a history.

**Notes only** mode (Points of Interest, Accommodation, Restaurants,
Embassies, Transport Hubs) reads just the name, country, city and Notes
columns, and ignores everything else. Use it when a researcher has
detailed notes on places that are already in TMS. It never creates
records: a name that matches nothing is listed as a Problem.

## Other spellings

When a row is merged into a record under a different spelling, that
spelling is saved as one of the record's **Alternate Names**, so the
next file that uses it matches automatically.
