<div align="center">
<picture>
  <source media="(max-width: 600px) and (prefers-color-scheme: dark), (min-width: 768px) and (max-width: 880px) and (prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/feder-cr/invisible_playwright_mcp/main/assets/banner-narrow-dark.png">
  <source media="(max-width: 600px), (min-width: 768px) and (max-width: 880px)" srcset="https://raw.githubusercontent.com/feder-cr/invisible_playwright_mcp/main/assets/banner-narrow-light.png">
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/feder-cr/invisible_playwright_mcp/main/assets/banner-dark.png">
  <img src="https://raw.githubusercontent.com/feder-cr/invisible_playwright_mcp/main/assets/banner-light.png" alt="invisible_playwright_mcp" width="729">
</picture>
<h3 align="center">Other AI browser agents get captchas.<br>
This one is invisible to anti-bots.</h3>
</div>

---

## Two ways to use this browser agent

### 1. From your assistant, over MCP

Windows, in PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
```

Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

Then install it in your assistant.

**Claude Code:**

```bash
claude plugin marketplace add feder-cr/invisible_playwright_mcp
claude plugin install invisible-playwright-mcp@feder-cr
```

**Codex:**

```bash
codex plugin marketplace add feder-cr/invisible_playwright_mcp
codex plugin add invisible-playwright-mcp@feder-cr
```

**Gemini CLI:**

```bash
gemini extensions install https://github.com/feder-cr/invisible_playwright_mcp
```

### 2. Standalone: the web UI

We bring the interface, you bring an [OpenRouter](https://openrouter.ai) key.
Chat on the left, the live browser on the right.

Windows, in PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
uvx invisible-playwright-mcp ui --openrouter-key sk-or-...
```

Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uvx invisible-playwright-mcp ui --openrouter-key sk-or-...
```

Then open **http://127.0.0.1:8765** and type the same thing.

---

## What to ask a web browsing agent

Anything that needs real web automation: a browser rather than an API, and a
person's judgement about what is on the page.

> Go to `<paste the URL>`. One way, Milan to Lisbon, economy, one checked bag,
> one adult. Check every date from the 12th to the 16th of next month, one at a
> time, and read the cheapest fare for each day. The date field is a calendar
> widget, so click the days rather than typing them. If a date has no
> availability, say so. Do not guess a number.

It drives the page the way a person would: the pointer moves, keys are pressed.

## Options: proxy, profile, seed

- **`--openrouter-key`** Your key, or the `OPENROUTER_API_KEY` variable.
- **`--model`** An OpenRouter model id, or `INVISIBLE_MCP_MODEL`. Defaults to `z-ai/glm-5.3-flash`.
- **`--proxy`** Optional. `http://user:pass@proxy.example.com:8080` or
  `socks5://proxy.example.com:1080`. Host and port are both required. The
  timezone, locale and egress follow it.
- **`--binary`** An engine binary you already have. It must be the build the seal
  pins, or startup refuses: this skips the download, not the version check.
- **`--seed`** An integer. Same seed, same browser identity, every run.
- **`--profile-dir`** A directory to keep the profile in, so logins and cookies
  survive restarts.
- **`--headed`** Show the browser window. The interface shows you the page anyway.
- **`--host`, `--port`** `127.0.0.1` and `8765`. Changing the host
  exposes an interface that has no authentication.

### A `.env` beside the command

Rather than retyping the key and the binary path, put them in a `.env` in the
directory you run from:

```
OPENROUTER_API_KEY=sk-or-...
STEALTHFOX_BINARY=/path/to/firefox
```

It is read at startup, and on the way in it **never overrides** something
already set, so the order is `--flag` > the environment > `.env` > the default.
Only the directory you are in is read - there is no search upwards, so running
from a subfolder cannot silently pick up a different key. The startup line names
the variables it applied and never prints their values.

Passing `--openrouter-key` puts the key in your shell history, and on Linux in
the process list. `OPENROUTER_API_KEY` in the environment or in a `.env` avoids
both.

### Shared mcpd process: isolated callers

Set `STEALTHFOX_OWNER_MODE=mcpd` only for a trusted mcpd stdio child. Each
transport session gets its own `main` and `support`, with separate Firefox
launches and private ephemeral profiles. Missing owner metadata is refused;
without this variable the existing single-owner behavior is unchanged.

`STEALTHFOX_MAX_BROWSERS` defaults to **2 across all callers**, including
launches and closes in progress. A full process refuses opens without evicting
anyone. `STEALTHFOX_OWNER_IDLE_SECONDS` defaults to **900**; idle browsers,
ended sessions and process shutdown close their browsers and remove profiles.
Persistent `profile` arguments and `file:` navigation are refused in this mode.

For uploads, configure `INVISIBLE_MCP_UPLOAD_DIRS` and copy files into the private
`upload dir` reported by `browser_open`/`browser_status`, not into the shared
root. It survives browser reopen and is removed when the owner ends. Upload
roots containing the profile temporary directory are refused at startup.
Owner mode requires POSIX locks. Each process holds a locked `stealthfox-proc-*`
instance directory under the temporary, first upload, and first download roots;
all owner directories live inside those instances. Startup removes only
same-UID instances whose lock can be acquired, so hot-reload generations can
overlap safely. Legacy flat directories are retained and logged.

For downloads, configure `INVISIBLE_MCP_DOWNLOAD_DIRS`. Each owner gets a
private `download dir` under its first root; `browser_download` saves only
there (or a non-hidden subdirectory), never in shared roots or another owner's
directory. Saved files survive browser reopen and are removed when the owner
ends. Both directory lines precede the final fill-handle line and are omitted
from handle-delegated status. Download roots have the same startup restrictions
and stale-directory cleanup as upload roots.

Shutdown attempts graceful closes for three seconds. An independent watchdog
observes stdin hangup and SIGTERM even if teardown blocks the event loop; after
3.5 seconds it attempts cleanup for at most one more second and exits unsuccessfully.
mcpd remains responsible for terminating the browser process group.

The owner's `browser_open` and `browser_status` disclose a generation-bound
**fill handle** for `main`, for delegation to a trusted credential filler.
Treat it as a secret; closing/reopening revokes it. See
[owner-mode configuration and the transport trust boundary](docs/mcp-server.md#shared-mcpd-owner-mode).

### Fork-only credential guard and downloads

`browser_type` accepts `expect_origin` and, with it, `expect_input_type`.
The engine checks both at every write. A first-focus model render can empty a
credential field; only an observed empty field permits a repeat, up to three
guarded writes. An unreadable or changed nonempty value fails without retry.
Success requires a stable equality readback and is exactly `typed into <selector>`,
never background progress or another typing's news. Plain typing retains
upstream's no-retry behavior. Credential values never appear in diagnostics.
This requires the companion fork engine's guard API; the ordinary upstream
engine cannot perform guarded writes.

`browser_type(mask_value=true)` also protects concealed values filled into text
inputs. It requires `expect_origin` and at least eight characters (empty clears
are allowed). Registration is Python-only, per browser, before writing, and is
forgotten on browser close or owner-session end. Every owner tool result,
including errors, masks raw, HTML/JSON-escaped and URL-quoted eight-character
fragments with eight bullets. Screenshot, watch and coordinate-click captures
refuse while the value is in an input, textarea or page text; unreadable presence
checks also refuse. A masked fill stops an existing live watch before writing.
As with password inputs, `browser_evaluate` can still compute other encodings
over `.value` (for example `btoa`); these plain-form masks are not a sandbox.

`browser_download` clicks a selector or point and keeps a completed file or a
document navigation, not a page's fetch/XHR. Set `INVISIBLE_MCP_DOWNLOAD_DIRS`
to absolute canonical directories, separated by the platform's path separator.
`save_to` is a directory inside those roots (only the caller's private directory
in owner mode). Empty files and incomplete responses are refused; saved files
are flushed and their size and SHA-256 read back before success. Firefox's
randomly named `.part` files keep a landing directory pending until completion.
Downloads work on Windows and POSIX; owner mode itself requires POSIX locks.

## The wiki: AI browser-agent guides

The reading room around the agent lives in the
[wiki](https://github.com/feder-cr/invisible_playwright_mcp/wiki): the
[AI browser-agent landscape: browser-use, Operator-style and
computer-use agents compared](https://github.com/feder-cr/invisible_playwright_mcp/wiki/guides-alternatives-and-comparisons),
[what to check when an agent gets blocked](https://github.com/feder-cr/invisible_playwright_mcp/wiki/why-does-my-ai-agent-get-blocked),
and [what happened to OpenAI Operator](https://github.com/feder-cr/invisible_playwright_mcp/wiki/is-openai-operator-still-available),
among others. Worked examples, transcripts and their outputs live in
[articles/](https://github.com/feder-cr/invisible_playwright_mcp/tree/main/articles).

## The rest of the family: engine, core

The MCP server from option 1 ships inside this package: `invisible_playwright_mcp` with no
subcommand is the server, `invisible-playwright-mcp ui` the interface. Its config blocks for
clients that take a file, its settings and its tools are on the wiki page
[The MCP server](https://github.com/feder-cr/invisible_playwright_mcp/wiki/mcp-server).

- **[invisible_playwright](https://github.com/feder-cr/invisible_playwright)**
  The engine, as a Python library, for writing code instead of prompts. The API
  is Playwright's.
- **[invisible_core](https://github.com/feder-cr/invisible_core)**
  Seed to fingerprint to preferences, proxy and geolocation.

## Using it responsibly

This automates a browser under your control. Read the terms of the sites you
point it at, respect their rate limits, and do not submit anything a human has
not read.

## Privacy Policy

This browser agent runs on your machine and has no server of its own. What
leaves your computer, and to whom:

- **The sites you visit** see the browser, as they would any Firefox.
- **Your model provider.** The web UI sends the conversation and what the agent
  reads on the page to OpenRouter under your key. Over MCP, the client you
  plugged it into does the same with whichever model it uses.
- **GitHub.** The engine is downloaded from a GitHub release the first time
  the server or the interface starts, and a GeoIP database is when a proxy is
  set. Each browser launch also fetches a one-line counter file from a GitHub
  release, which is how launches are counted: the request carries no
  identifier and nothing of yours, and GitHub sees what any HTTPS request
  shows, your IP address.

Nothing else is collected and nothing is sent to the author. Sessions,
profiles and screenshots are stored locally, under `INVISIBLE_MCP_HOME` if set and
otherwise in the application-data directory of your system, and are yours to
delete; nothing is retained anywhere else. Questions go to the
[issues](https://github.com/feder-cr/invisible_playwright_mcp/issues).

## License

[MIT](https://github.com/feder-cr/invisible_playwright_mcp/blob/main/LICENSE). Everything
distributed before 2 September 2026 was released under AGPL-3.0 and stays under
it.

<!-- The Official MCP Registry verifies ownership of a PyPI package by finding
     this token in the published description, which is this file. It must match
     the `name` in server.json exactly. -->
<!-- mcp-name: io.github.feder-cr/invisible-playwright-mcp -->
