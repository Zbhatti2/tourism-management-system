---
title: TMS Agents
order: 1
keywords: [agents, ai, platform, geography, distances, drive time, points of interest, enrichment, review, approve]
---
**TMS Agents** are platform-level AI agents, a **TMS System Admin**
screen. They research the shared catalogs on the web and **propose**
values. Nothing is saved until you approve it.

## The agents

- **Geography:** city coordinates, IANA time zone and altitude, for
  cities that are missing them. Choose a country (and optionally a
  province) and how many cities.
- **Distances & Drive Times:** road distance, typical drive time (as a
  range when sources give one), the main route (M-2, N-5, KKH …) and
  whether a passenger train runs. Choose one city and several
  destinations, or **fill the gaps** in the distances you already have.
- **POI Enrichment:** description, year founded, opening hours, entry
  fee, website, phone, address and coordinates for Points of Interest
  with gaps. Choose a city and/or POI type.

Runs are kept small on purpose: up to 40 cities, 30 pairs or 24 POIs. A
run takes about a minute per batch, and its page refreshes by itself.

## Reviewing

Each proposed value shows:

- what the catalog has now;
- what the agent found;
- a **confidence** (green 80%+, amber 50–79%, red below 50%);
- the **source page**. Open it before approving anything you're unsure
  of.

Tick values and click **Approve ticked** or **Reject ticked**.
**Approve all ≥ 80%** approves every high-confidence value on the page.
The **Review queue** collects everything still waiting, from every run.

Approved values are written straight to the catalog. They're dated
(*Checked On* or *Verified On*), and the source is recorded when the
record had none. Points of Interest changes then reach tenants through
**Catalog Sync**.

## POI Image Collector

The **POI Image Collector** card opens the POI Master Image Catalog's
import page. There the agent finds photos of platform Points of
Interest, and you curate them before tenants receive them. See *Platform
Catalogs → POI Master Image Catalog*.

## Watching a run

A run's page shows how long it has been running, a progress bar, and
when it last did something. **Stop** ends it; values already found stay
in the review queue. A run with no activity for 10 minutes is stopped
automatically.

## Cost

TMS Agents are a platform cost. They appear on the **Platform agents**
line of **AI Usage**, never on a tenant's. They use the
`PLATFORM_ANTHROPIC_API_KEY` setting when it's set, otherwise the main
`ANTHROPIC_API_KEY`.
