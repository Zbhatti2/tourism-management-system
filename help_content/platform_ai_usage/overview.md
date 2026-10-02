---
title: AI Usage — all tenants
order: 1
keywords: [ai, usage, billing, allowance, tenants, cost]
---
This **TMS System Admin** page shows each tenant's AI spend for the
month, which is billable to that tenant. Spend by platform agents is
shown separately, as a platform cost.

Costs use Anthropic's list prices, kept in `ai_usage.py`. Web searches
are charged at $10 per 1,000.

**Allowance:** enter a dollar amount for a tenant and click **Set** to
cap its monthly AI spend. Leave the box blank for no limit. A tenant that
reaches its allowance can't start new agent runs until the next month.
