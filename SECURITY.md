# Security

## Reporting a problem

Please email **roushan9234620012@gmail.com** instead of opening a public issue. Include what you found, how to reproduce it, and
which version you use (Settings shows it). You'll get a reply within a few days, and a fix is released as soon as
it's ready. You'll be credited in the release notes unless you'd rather not be.

Only the latest release gets security fixes. Update from the [Releases page](https://github.com/KumarRoushan9234/Keirolake/releases).

## How KeiroLake protects your data

**On your computer**

- Everything lives in one folder that only your user account can read. Nothing is uploaded unless you turn on a cloud
  model or web search.
- The engine listens only on `127.0.0.1`, on a new port with a new secret token every launch, and checks the `Host`
  header, so websites open in your browser can't call it (including through DNS rebinding).
- API keys you enter for cloud providers are kept in your system's keychain, not in a file.
- The Vault encrypts private notes with AES-256-GCM and a key derived from your passphrase with scrypt. The key lives
  only in memory, the Vault locks after 15 idle minutes, and Vault notes are only sent to models on this computer.
- Backups can be encrypted the same way. "Test restore" opens a copy in a temporary folder to check it.

**When you share it**

- Sharing is off by default. Every way in except this computer needs a password of at least 10 characters.
- Five wrong passwords lock a visitor out for five minutes, and each visitor is rate limited.
- Home Wi-Fi and open-port sharing use HTTPS. An open port can be limited to an IP allow-list.
- The internet link uses Cloudflare's tunnel program, checked against its published SHA-256 before it runs. It opens
  no ports on your router.
- The app's own management API (`/api`) only answers this computer. Other devices get the web app after signing in,
  plus the data API and MCP with a key.

**When your apps connect**

- API keys carry only the permissions you choose (`read`, `ask`, `write`, `sql`, `llm`), can be limited to certain
  collections, can expire and can be revoked.
- Keys are stored only as SHA-256 hashes. Each key is limited to 120 calls a minute, and every call is logged.
- SQL runs on a read-only connection and can read only your own tables, never keys or webhooks.
- Webhooks are signed with HMAC-SHA256 (`X-KeiroLake-Signature`).

**What it downloads**

- Models are read as data (GGUF and safetensors). Pickle files, `.bin`/`.pt` weights and repository scripts are never
  downloaded or run.
- Offline library pages are shown with a policy that blocks scripts. Web pages opened in the Reader are fetched only
  from public addresses.
- Heretic is not bundled. When you point the app at your own copy, it runs in its own private environment.
