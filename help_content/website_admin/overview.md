---
title: Website Overview
order: 1
keywords: [website, public site, domain, mavietours, groups, tours, departures, booking, enquiry, inbox, chat, policies, deposit, publish]
---
Your public website is its own small project (for Ma Vie Tours, the
`ma-vie-tours-website` repository, running as its own app in Coolify on
`www.mavietours.com`). It fills itself from your tour data in TMS through
the **Website API**, so nothing is typed twice: the itinerary, hotels,
restaurants and Points of Interest come straight from the Package. Costs,
supplier contacts and internal notes are never shown.

The **Website** menu in the sidebar has four screens. Use it with your
tenant login (e.g. Zeb), not the Platform Admin.

## Settings & Groups

Each section has its own **Save** button; saving brings you back to the
same section.

- **Site details**: title, banner text, About Us text, contact email,
  phone, WhatsApp number, address, colours, logo and background image.
- **Domain**, e.g. `www.mavietours.com`: where your website runs. Only
  this domain (with or without `www.`) may show your tours.
- **Connection to your website**: the two values your website needs, by
  the names they have in its `js/config.js` — **TMS address (TMS_BASE)**,
  `https://tms.ipromise.com`, and **Website key (SITE_KEY)**. Use **Copy**.
  They go in the website's code, **not in Coolify**. The key is not a
  secret; **Make a new key** replaces it, and the website stops working
  until its `js/config.js` has the new key and it is redeployed.
- **Test addresses**: other web addresses that may show your tours, live or
  not — e.g. `http://localhost:8080` while trying the site on your PC.
- **Payments and policies**: payment methods, payment terms, deposit,
  booking terms, cancellation, refunds and privacy. Sensible wording is
  filled in to start with — edit it to your own.
- **Live**: your domain shows your tours only once *Website is live* is
  ticked. Before that, only the test addresses can, and the website shows
  a yellow strip: *"This website is not live yet…"*. To preview on your
  own domain without going live, add it (e.g. `https://www.mavietours.com`)
  to the test addresses.
- **Groups**: each Package Group can have a box on the home page with an
  image and caption (e.g. *Sikh Gurdwaras*, *Northern Pakistan*). Tick
  *Show on the website* for the groups you want.

## Tours on the website

Every Package is listed. Open one (or use **Website** on the Package page)
to set what the public sees:

- Title, tagline, overview, highlights, who it's for, requirements, packing
  list, what's included and excluded, difficulty, lodging, group size.
- **Price (US$)** per person, single room supplement and deposit. A tour
  needs a price before you can tick **Publish on the website**.
- **Photos** of the tour. Photos of its Points of Interest and hotels are
  added after your own automatically.
- **Departures**: start date, price (if different), seats and booking
  deadline. Visitors can book only open departures before the deadline
  with seats left.

## Website Inbox

Enquiries and booking requests from the site land here (and are emailed
to you when email is set up). A booking is a *request*: no payment is
taken online. Set the status as you follow it up — **Confirmed** adds the
travellers to the departure's seats taken.

## Website Chats

Visitors can chat on the site. The AI assistant answers from your
published tours and policies; its cost counts towards your AI usage, with a
limit per visitor. When a visitor asks for a person (or the assistant
can't help), the chat shows as **Waiting** here. **Take over** to reply
yourself, **Hand back to the AI** to return it, or **Close** it.

## When the website shows a yellow strip

- *Test view: this website is not live yet* — it works, from a test
  address; tick **Website is live** when you're ready.
- *This website is not live yet. Switch it on in TMS…* — tick **Website is
  live** (or add the domain to the test addresses).
- *This website's key is not recognised by TMS* — `SITE_KEY` in the
  website's `js/config.js` doesn't match the **Website key** here.
- *This web address may not use this website's data* — the address isn't
  your **Domain** or one of the test addresses.
- *Our tours are coming soon* (no strip) — nothing is published yet:
  publish a tour and give its Group a box.

## Setting up a tenant's website (once)

The website is its own repository (e.g. `Zbhatti2/ma-vie-tours-website`)
and its own application in Coolify, like the GSS tenant websites.

1. **Coolify → Keys & Tokens → Private Keys → + Add**: generate an
   ED25519 key (e.g. *Tenant-10000002-Ma-Vie-Tours Website*) and copy its
   public key.
2. **GitHub → the website repository → Settings → Deploy keys → Add deploy
   key**: paste it, read-only.
3. **Coolify → the tenant's website project → + New Resource → Private
   Repository (with Deploy Key)**: that key,
   `git@github.com:Zbhatti2/<repository>.git`, branch `main`, Build Pack
   **Dockerfile**.
4. On the application: **Ports Exposes** `80`; add the domains one at a
   time without `https://` (`www.mavietours.com`, then `mavietours.com`),
   port 80. Leave **Port Mappings** empty. **Deploy**.
5. DNS at the domain's registrar: **A** records for `@` and `www` to the
   VPS2 server's IP (the same as tms.ipromise.com).
6. Here: set the **Domain**, put the **Website key** in the website's
   `js/config.js`, then tick **Website is live**.
