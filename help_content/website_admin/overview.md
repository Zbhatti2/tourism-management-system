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

The **Website** menu in the sidebar has four screens.

## Settings & Groups

- **Site details**: title, banner text, About Us text, contact email,
  phone, WhatsApp number, address, colours, logo and background image.
- **Domain**, e.g. `www.mavietours.com`: where your website runs. Only
  this domain (with or without `www.`) may show your tours.
- **Connection to your website**: the **TMS address** and the **Website
  key** your website needs (in its `js/config.js`). Use **Copy**. The key is
  not a secret; **Make a new key** replaces it, and the website stops
  working until its `js/config.js` has the new key and it is redeployed.
- **Test addresses**: other web addresses that may show your tours, live or
  not — e.g. `http://localhost:8080` while trying the site on your PC.
- **Payments and policies**: payment methods, payment terms, deposit,
  booking terms, cancellation, refunds and privacy. Sensible wording is
  filled in to start with — edit it to your own.
- **Live**: your domain shows your tours only once *Website is live* is
  ticked. Before that, only the test addresses can.
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
