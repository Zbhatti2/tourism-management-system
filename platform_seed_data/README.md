# Platform seed data

Reference data loaded into the shared (platform-level) tables by run-once
migrations in `db.py`. Files here are read only by those migrations.

## airports_ourairports.csv

Major airports for **Transport Hubs**: every large or medium airport with
scheduled passenger service and an IATA code, from
[OurAirports](https://ourairports.com/data/) (public domain), downloaded
2 October 2026. Columns: IATA, ICAO, name, size (large/medium), latitude,
longitude, ISO country, ISO region code and name, municipality, website,
Wikipedia link.

Loaded by migration `2026_10_load_major_airports`. Re-running it is safe:
an airport whose IATA code is already on file only has its empty fields
filled in, so edits made in TMS are never overwritten.
