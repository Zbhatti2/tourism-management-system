---
title: Package Management Overview
order: 1
keywords: [packages, group, package groups, tours management, tour package, template, itinerary, services, products, suppliers, points of interest, airport, station, transport hub, airline ticket]
---
A Package is a tour package **template** — assembled from Services,
Products, Suppliers, and Points of Interest, but not yet a scheduled
departure or a customer booking (those are later roadmap phases).

Package Management and Tour Design are under **Tours Management** in the
sidebar.

## Groups

Each package can belong to a **Group**, such as *Gurdwaras Tour* or
*Northern Areas*. The list shows a **Group** column, and **All groups**
filters by one (or *No group*). Set the group on the package's Edit
screen. A package added from a Tour Design takes the design's group. The
groups themselves are kept in **Table Maintenance → Package Groups**, where
you can add, rename, reorder or deactivate them.

## Creating a package

**New Package** offers two ways:

- **Create New Package**: start from a Tour Package service (Inventory
  Management → Services) and build the route, days and components
  yourself.
- **Add from Tour Design**: lists the Tour Designs marked **Ready for
  package**. Tick one or more; each becomes its own draft Package, with:
  - the design's overnight checkpoints as the route, with their nights;
  - the day-by-day itinerary, including the guest text, visits, meals and
    team notes;
  - each day's visits linked to your Points of Interest where the names
    match;
  - the chosen hotel at each checkpoint as Hotel/Room lines (rooms ×
    nights, with the price and supplier when known).

  The Package's notes say which design and version it came from, and list
  any flags that were still open. Check its details, prices and
  components before making it active.

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
