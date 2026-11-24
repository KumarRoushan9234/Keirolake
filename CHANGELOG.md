# Changelog

Each release on GitHub uses its section here. Add notes under **Unreleased** as you work; they go out with the next
automatic release.

## Unreleased

## 0.9.0

**macOS**
- Mac builds for Apple Silicon and Intel (`.dmg`), built and engine-tested on every release, and updating themselves.
- The internet link, keep-awake (`caffeinate`) and Ollama setup work on a Mac.

**Phone-first**
- **Capture**: voice notes turned into text on your own computer (local Whisper), photos of pages read by a local
  vision model, links saved as readable pages, and notes, all into a "Captured" collection.
- **Share to KeiroLake** from any Android app, and captures made with no signal wait on the phone until the lake is
  reachable again.
- A microphone button in Ask and Build with AI.
- A bottom tab bar on phones (Ask, Capture, Build, Tables).
- Photos are never sent to Ollama's cloud models, only to models that run on your computer.

**Updates**
- `.deb` and `.rpm` installs update from Settings → Updates with one click (your system asks for your password);
  each package is checked against the project's signature first.

**Releases**
- A hang in the Linux package test no longer blocks releases. Every job has a time limit, and the build actions use
  current versions.
- A demo video at the top of the README.

## 0.8.0

**Work keeps going when you switch pages**
- Build with AI conversations live in the engine: leave the page or close the window, and the reply or the
  filled table is waiting. Every build is kept in a list you can reopen, also after a restart.
- Asking a sheet, planning a table, reading web pages into a table, cleaning and the gateway's "Try it" keep running
  in the background. A notice appears when they finish, with a button to open the result.
- The jobs bell counts everything running, including requests started from a page.

**Better Build with AI**
- Edit the plan by hand: rename, retype, reorder and remove columns or sections.
- Starter ideas, web search as a source, and a preview of the latest rows with how sure each one is.
- Add rows or columns to a table you already have.

**Updates without a server**
- The Windows app and the Linux AppImage update themselves from GitHub Releases, with signature checks.
  Settings → Updates: check now, install, or install automatically.

**Run it anywhere**
- `keirolake serve`: the full engine, web app, API, MCP and internet link with no desktop window, for home servers,
  NAS boxes and cloud VMs.
- A one-container Docker image (`docker-compose.server.yml`).
- The web app installs on phones and computers (Add to home screen / Install).
- New CLI commands: `status`, `link`, `keys`, `tables`, `build`, `update`.

**Look and feel**
- Choose an accent colour: blue, violet, green, orange or rose.

**Releases**
- Every push to main builds, tests and publishes Windows and Linux installers automatically.

## 0.7.4

- Build with AI: describe a table or an offline library in a chat and the model fills it.
- The internet link works with home Wi-Fi sharing on, comes back after a restart, and can use your own Cloudflare
  tunnel for a fixed address.
- Keep the computer awake while the app runs, and optionally stay on with the lid closed.
