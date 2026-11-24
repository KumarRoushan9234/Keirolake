# Data pipelines (KeiroLabs only)

A pipeline collects web data with **KeiroLabs and nothing else**, pulls fields out of each page, and
stores the result in a table, a knowledge base, or both.

```
goal in plain English ──► plan (steps + fields + outputs) ──► dry run ──► save ──► run / schedule
                                  │
                                  └─ steps are KeiroLabs calls only: search · extract · sitemap · research
```

## What to tell it

Say five things. It's fine to leave some out; the planner fills sensible defaults.

1. **What** to collect (news, prices, job posts, documentation…)
2. **Where from**: a topic, or specific sites and pages
3. **Fields** you want as columns, with their kind (text, number, date, yes/no, link)
4. **Where it goes**: a table you can query with SQL, a knowledge base you can chat with, or both
5. **How often**: once, daily, weekly

Template:

> Collect *[what]* about *[topic or sites]*. For each one, I want *[field, field, field]*.
> Save it to *[a table / a knowledge base]*. Repeat *[how often]*.

Examples that work well:

| You say | It plans |
|---|---|
| "Every day, collect new articles about electric vehicle launches. For each I want the company, model, price in USD and launch date." | `search` (content mode) → fields `company`, `model`, `price_usd`, `launch_date` → table, daily |
| "Each week, read the pricing pages of notion.so, coda.io and airtable.com and record plan names, monthly price and key features." | `extract` of the three pages → fields → table, weekly |
| "Collect the newest posts from qdrant.tech/blog into a knowledge base so I can ask questions about them." | `sitemap` (path `/blog`) → pages read → knowledge base |
| "Research how cities handle flood warnings and keep the sources in a knowledge base." | `research` → knowledge base |

Tips: name the fields you want ("price in USD", "launch date"); name sites when you know them; say "knowledge base"
if you want to *ask questions* later and "table" if you want to *count, sort and filter*.

## The four KeiroLabs steps

| Step | Does | Notes |
|---|---|---|
| `search` | Web search. Mode `content` returns full page text, `fast` and `lite` return snippets (cheaper) | up to 10 results |
| `extract` | Reads specific pages you name | up to 20 URLs |
| `sitemap` | Lists a site's newest pages (optionally under a path), then reads them | up to 25 pages |
| `research` | Deep research across sources | slowest, 2 credits per call |

A run reads at most 30 pages. Before a run, the plan shows how many KeiroLabs calls it makes.

## Why it is KeiroLabs-only

- The plan is checked by `validate_spec` before anything runs. A step of any other kind (`http_get`, `scrape`, a browser, a shell,
  SQL…) is rejected with a message, whether it came from the planner or was typed into the JSON editor.
- `app/pipelines.py` doesn't import any HTTP or scraping library, and a test fails if one is ever added. The only way it reaches
  the web is the KeiroLabs client.
- A model reads the *page text KeiroLabs returned* to fill your fields. That's the only other thing involved, and it can be a
  model on your own computer.

## Running

1. **Plan it.** Review the steps, fields and outputs. Edit the JSON if you want exact control.
2. **Save pipeline**, then **Dry run**. It collects and extracts but writes nothing, and shows sample rows.
3. **Run now**, or set a schedule (hourly to weekly). Runs go to the background queue, so you can keep working.

Where the data lands:

- **Table** `ds_<name>`: one row per page, with `source_url`, `title`, `collected_at` and your fields. Re-runs *update* a page's
  row instead of adding a duplicate. Query it on the Warehouse page, in plain English or SQL.
- **Knowledge base**: the page text is indexed like any other document, with its link, so Ask can cite it.
- **Lake**: the untouched KeiroLabs response of every run is kept in `lake/raw/pipeline/` and recorded in the lineage.

## Limits and honesty

- Extraction is done by a model and can be wrong. It is told to answer `null` rather than guess, and a row with every field empty is
  dropped, but check the dry-run sample before trusting a new pipeline.
- Credit prices depend on your KeiroLabs plan, so the app shows call counts, not a price.
- A pipeline never fetches pages that KeiroLabs can't read.
