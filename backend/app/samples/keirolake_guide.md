# KeiroLake guide

KeiroLake is a private data lake that runs on your computer. It keeps tables, documents and offline libraries in one place,
answers questions about them with local AI models first, and lets your other apps and devices use the same data.

## Where things live

- **Tables** hold rows and columns. You can edit them like a spreadsheet, fill a column with the local model or the web,
  clean them, chart any column, and export CSV, Excel, JSON or PDF. Every table is also a SQLite table you can query.
- **Collections** hold documents (PDF, Word, Markdown, web pages). Their text is split into passages and embedded, so you
  can search them by meaning. Tables found inside uploaded CSV, Excel and PDF files also become tables.
- **Topics** gather everything about one subject: KeiroLabs finds the pages, KeiroLake builds a clean table with a link to
  each source, and keeps the pages searchable.
- **Libraries** are offline references such as Wikipedia, read without the internet.
- **Files** shows the lake's folders on disk: raw (as it arrived), clean, and curated (exports, topic CSVs, history).

## Asking questions

Ask answers from your tables, collections and libraries first, and reaches for the web only when your data has nothing on
the question (you can turn the web off). Answers cite their sources, and Research and Deep research check each claim
against the page it came from. Every answer can be saved to the lake or turned into a table.

## Using the lake from other apps

- **LLM gateway**: an OpenAI-compatible address (`/v1/chat/completions`, `/v1/embeddings`, `/v1/models`). Any OpenAI SDK
  works with it. The model `lake` answers from your data and returns its sources.
- **Tables API**: `/v1/tables` with filters, saved views at `/v1/views/<name>`, and search by meaning.
- **MCP**: the same tools for AI assistants such as Claude.
- Every call needs an API key, made in Serve > API keys, with only the permissions that app needs.

## Reaching it from other devices

Sharing has four ways in: this computer only, home Wi-Fi, an internet link (a Cloudflare tunnel, no router changes), or a
port you open yourself with HTTPS, an allow-list and optional router port-forwarding. Every way except this computer needs
a password, and repeated wrong passwords lock that visitor out for five minutes.

## Models

Models lists the local models and lets you download one at the precision you want (Q2 to F16), check whether it fits your
graphics memory, and re-quantize a model you have. Heretic can remove a model's refusals; the result can be imported into
Ollama. Cloud models are optional, used only with a key you add, and limited per day.

## Sample data

This guide and the World countries table are sample data. Remove them in Settings > Data and storage whenever you like.
The countries table comes from the World Bank (CC BY 4.0): population, GDP per person, life expectancy and CO2 per person.
