---
title: Data Exchange Overview
order: 1
keywords: [data exchange, import, export, csv, staging, commit, batch]
---
Data Exchange (under **Tables & Utilities**) is where bulk imports and
exports happen.

## Importing

A CSV upload never lands directly in the live tables. It becomes an
import batch: each row is staged and validated first, and you review the
whole batch before anything is committed. Only an explicit **Commit**
writes the reviewed rows into the real records (Contacts, and other
modules following the same pattern). Nothing changes silently on upload —
you always see what will be created before it happens.

## Exporting

Every export is recorded as a job, with the file written to the server
and then served back to you for download — so there's a record of what
was exported and when. Exported files contain your data decrypted to
plain CSV/JSON (it's your own backup of your own data), so store and
delete downloaded exports carefully.
