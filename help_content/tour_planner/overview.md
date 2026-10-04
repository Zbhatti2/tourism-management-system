---
title: Tour Planner
order: 1
keywords: [tour planner, ai agent, itinerary, route, checkpoints, journey times, brief, package]
---
The **Tour Planner** is an AI agent that plans a group tour with you, one
stage at a time, towards a draft Package. Open it from **AI Agents → Tour
Planner**.

## Starting a plan

Click **New tour plan**, give it a name and paste the request as you
received it, for example: group size, where people arrive from, the dates, the
route and the standard. Click **Read the request into a brief**. The agent
fills in the **Tour Brief** and lists anything it needs to ask. You can also
start with an empty brief and fill it in yourself.

## The stages

Each stage shows its status on the left: *Ready*, *Working…*, *To review*,
*Approved*. A stage runs only after the stages before it are approved.

1. **Tour Brief**: the tour, its dates and length, the travelling parties
   and their rooms, guides, vehicles and drivers. TMS works out the rooms
   and the vehicles for you; with one guide per minibus, 24 guests fill
   two 14-seaters, 12 guests and a guide in each. **Save and approve the
   brief** to go on.
2. **Route and POIs**: the main route city by city, with distances and drive
   times, the attractions on and near it, and the detours they need. TMS's
   own distances are used wherever they exist; anything from the web shows
   its source. The cumulative km and drive time from the start are shown
   beside each leg. Cities TMS flags as overnight checkpoints are marked.
   Untick the points of interest you don't want to plan for.
3. **Checkpoints**: the overnight cities, nights in each, and the day budget
   against your maximum driving per day. A day over the limit is
   highlighted. If the plan doesn't fit the tour length, it says so.
   Before running, set the **Checkpoint criteria**: the most (and least) km
   between checkpoints, hotel/stay requirements, food and meals, points of
   interest, and anything else. The agent says how each stop meets them and
   lists the other cities it considered. It may pick a city TMS doesn't flag
   as a checkpoint; when you approve the stage, those cities are suggested
   to the platform (TMS Agents review queue) to be flagged. Saving new
   criteria clears the checkpoints so the agent can choose again.
4. **Journey grid**: worked out from the route and checkpoints, with the drive
   times between every pair of checkpoints and city by city along the
   route.

5. **Hotels and dining**: for each checkpoint, 2–3 hotels that can take the
   whole group, with one recommended:
   - your own Suppliers come first, with their prices from TMS, then the
     platform catalog, then the web;
   - hotels below your standard are marked;
   - choose a different hotel with the **Use** button, or type a price;
   - restaurants that can seat the group are listed with typical lunch and
     dinner costs;
   - the **Accommodation budget** totals rooms × nights × price for the
     chosen hotels, with guide and driver rooms priced as singles.
6. **Day by day**: each day's timed schedule, where breakfast, lunch and
   dinner are taken, the overnight hotel, a short description for the
   guests and notes for your team. You can edit the titles, descriptions
   and notes.

Arrivals, transport, Pre-Tour planning, costs and the draft Package are
later phases.

## Changing a stage

Type what you want in **Ask for changes**, for example "stop at Abbottabad
instead of Mansehra", and click **Re-run**. Changing or re-running a stage
clears the stages after it, which need redoing.

## Answering the flags

The agent lists **flags**: things to check or decide (a pass that may be
closed, a figure it had to estimate). Under each flag choose **Open**,
**Noted, I'll handle it**, **Checked / resolved** or **Not an issue**, and
add your answer or instruction. **Save answers** keeps them without
changing or un-approving the stage, and the agents of the later stages
read them. **Save and ask the agent to revise this stage** saves them and
re-runs the stage with your answers.

## Cost

Every run is charged to your organisation's AI Usage. A route or
checkpoint run is usually 10–30 cents. The plan's page shows the cost so far.
Distances the agent finds that TMS doesn't hold are sent to TMS for review,
so they can be added to the shared catalog.
