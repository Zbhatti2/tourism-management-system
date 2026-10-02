---
title: Transport Hubs Overview
order: 1
keywords: [transport, hubs, airport, railway, station, bus, terminal, seaport, ferry, iata, icao, platform, system admin]
---
Transport Hubs is a **TMS System Admin** screen. It holds one shared list
of the airports, railway stations, bus terminals and seaports / ferry
terminals that every tenant uses. Only TMS maintains it; tenants pick
from it.

## The list

Use the type buttons along the top to show one kind of hub, and the
search box to find a hub by name, code, city or operator. Untick
**Active** on a hub to hide it from tenants without deleting it.

The list shows up to 500 hubs at a time; search or pick a type or
country to narrow it down.

## Airports already loaded

About 3,200 airports are pre-loaded from OurAirports (a free,
public-domain airport list): every large or medium airport with
scheduled passenger flights and an IATA code. Large airports are marked
**Major**. Province/state and city are matched to TMS geography where
the name matches; otherwise they're kept as text. Edit any of them like
a hub you added yourself.

## Adding or editing a hub

- **Type** decides which fields apply. For an airport, **Code** is the
  3-letter IATA code (LHE) and an **ICAO code** (OPLA) can be added too.
- **Location** uses the same Region → Country → Province/State → City
  picker as the rest of TMS.
- **Coordinates**: paste them straight from Google Maps, either as decimal
  degrees (31.519346, 74.409302) or degrees/minutes/seconds. They're used
  for maps, distances and "nearest airport/station".
- **Major hub** marks the main airports and stations.

## Hub types

**Hub Types** (top right) lists the kinds of hub. Adding one (Metro
Station, Heliport) is all it takes to start listing hubs of that kind.
