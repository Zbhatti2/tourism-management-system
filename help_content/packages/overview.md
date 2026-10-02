---
title: Package Management Overview
order: 1
keywords: [packages, tour package, template, itinerary, services, products, suppliers, points of interest, airport, station, transport hub, airline ticket]
---
A Package is a tour package **template** — assembled from Services,
Products, Suppliers, and Points of Interest, but not yet a scheduled
departure or a customer booking (those are later roadmap phases).

## Building a package

A package brings together line items from the other catalogs:

- **Services and Products** — the bookable line items, pulled from
  Inventory Management's two catalogs.
- **Suppliers** — the vendors fulfilling those line items (a hotel for
  lodging, a transport company for transfers, and so on).
- **Points of Interest** — the attractions the itinerary visits.

## Airports, stations and terminals

Route stops and Airline Tickets use the shared **Transport Hubs** list
that TMS maintains (about 3,200 airports, plus railway stations, bus
terminals and seaports):

- On a **route stop**, *Arrive via* and *Depart via* record the hub the
  group comes in and leaves through. Hubs in the stop's country are
  suggested first.
- On an **Airline Ticket**, *From airport* and *To airport* record the
  flight's route; the Airline Tickets section then shows it as, say,
  LHE → DXB.

Type a name, code (LHE) or city and pick from the list. If a hub you
need is missing, ask your TMS system administrator to add it.

## What a Package is not, yet

Departures (specific dated instances of a package) and Bookings
(customer reservations against a departure) are both on the roadmap but
not built yet — today, a Package is a reusable template you assemble
once and can review or edit later, not something a customer books
directly.
