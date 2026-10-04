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

## POIs from a PDF

Upload PDFs, such as a brochure, guidebook or heritage report, on the **POIs
from a PDF** card. First choose what the PDF holds:

- **Several POIs per PDF**, such as a guide or a numbered list. The agent
  reads every place the pages describe.
- **One PDF per POI.** You can upload several files at once. Name each file
  after its POI, the same way as photos: `Gurdwara Rori Sahib.pdf`,
  `Gurdwara Rori Sahib - 2.pdf`. All of a file's text and photos go to that
  POI. If the name doesn't match a POI, the agent takes the name from the
  document.

Scanned PDFs and PDFs printed from a web page work too, because the agent
reads the pages as images.

What the agent takes from each place:

- **Description:** two or three sentences, for itineraries.
- **Notes / History:** everything else the document says, such as its
  history, the people and events, its land and its condition today. This is
  kept in the document's own words, with the file and page.
- **Additional links:** video, photo and document links.
- **Coordinates:** in any format, including degrees, minutes and seconds.
- **Year, hours, fee, website, phone and address,** where the document gives
  them.

How places are matched:

- **Places already in the catalog** are found by name: the same name, a
  name that sounds alike (Patshahi / Patshai), or a shorter or longer form
  of the name in the same city. Coordinates within about 1 km also count.
  A match by a similar name is marked **check** in the review.
- **New places** are proposed as a **New Point of Interest**. The POI is
  created when you approve any of its values. If the PDF doesn't state a
  type, the POI Type you chose on the upload form is used.

**Duplicate text.** Text is never proposed twice:

- The same place described twice in one upload becomes one entry, and
  repeated sentences are left out.
- For a place already in the catalog, the new text is compared with what
  the POI holds: its description, its notes, and text still waiting for
  review.
  - Sentences already there are dropped.
  - What's left is checked for meaning. If it's the same facts in other
    words, nothing is proposed. If it adds facts, only those sentences are
    proposed, and they are **added** to Notes / History, never replacing
    it. If a fact disagrees with what's stored, it is listed beside the
    proposal for you to decide.
- A PDF that was already read is refused unless you tick **Read it again
  anyway**.

**Photos.** Small photos are kept, photos the PDF split into strips are put
back together, and logos are left out. The agent sees each photo with its
page and position, and assigns it to the place it shows. This works even
with several places on a page. **Curate the photos** on the run's page
opens them. A photo of a new place is assigned once you approve that place.
Duplicate photos are fine; delete them while curating.

**Sources.** Each value shows the PDF page it came from, and **Open the
PDF** opens the file.

Up to 300 pages and 40 MB per upload. It costs roughly 1 to 3 cents a page,
as a platform cost.

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
