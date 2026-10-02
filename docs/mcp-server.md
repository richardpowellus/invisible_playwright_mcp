---
title: "The MCP server"
description: "The stealth Firefox as an MCP server, shipped inside invisible_playwright_mcp: the config block for every client that takes a file, the STEALTHFOX_* settings, and the tools with what each one returns."
parent: "Using the Agent"
nav_order: 29
---
# The MCP server

A stealth Firefox as an [MCP](https://modelcontextprotocol.io) server. Add it to
Claude Code, Claude Desktop, Codex, Cursor or any other MCP client, and your
assistant gets a real browser: navigation, reading, clicking, typing,
dropdowns, keys, screenshots, a live view of the window, and a JavaScript
reader, on a Firefox whose fingerprint is set inside the engine rather than
bolted onto the page.

If you are still deciding whether you want a server at all rather than a
library call, the price is the thing to weigh and it is a recurring one:
[MCP against a plain API](model-context-protocol-vs-api-vs-rag.md) has this
server's per-turn bill measured.

The engine is [`invisible-playwright`](https://github.com/feder-cr/invisible_playwright),
a Firefox patched at the C++ source. The server ships inside the `invisible_playwright_mcp`
package and is what `invisible_playwright_mcp` runs with no subcommand: `uvx invisible-playwright-mcp` is what a
client registers, `python -m invisible_playwright_mcp` is what the interface spawns. Every tool
below is a thin wrapper over the engine, and the interface (`invisible-playwright-mcp ui`) is a
client of it like any other.

**How to install this, and the two ways to use it, are in
[invisible_playwright_mcp's README](https://github.com/feder-cr/invisible_playwright_mcp#readme).** This page keeps
what the server itself owns: the config block for clients that take a file, the
settings, and the tools.

## Adding it to your client

Claude Code and Codex install it as a plugin, Gemini CLI as an extension, and
all three are in invisible_playwright_mcp's README. The rest take a config
file, and the file is not the same
everywhere: **three different top-level keys, and one of them is not even
JSON.** Find yours below. The block only tells the client how to start the
server; installing `uv` comes first, as invisible_playwright_mcp's README shows. The engine, the
server downloads on its own the first time it starts, and `browser_open`
reports the progress until it is there.

### If your client takes a config file

**Most use a top-level `mcpServers`** - Claude Desktop, Cursor, Windsurf, Cline:

```json
{
  "mcpServers": {
    "stealth": {
      "command": "uvx",
      "args": ["invisible-playwright-mcp"]
    }
  }
}
```

| Client | File |
|---|---|
| Claude Desktop (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Desktop (Windows) | `%APPDATA%\Claude\claude_desktop_config.json` |
| Cursor | `.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` for every project |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| Cline | `~/.cline/data/settings/cline_mcp_settings.json`, or the **Configure MCP Servers** button in its MCP panel, which opens whichever file your version uses |

**Zed calls the key `context_servers`**, not `mcpServers`, in
`~/.config/zed/settings.json` (`%APPDATA%\Zed\settings.json` on Windows):

```json
{
  "context_servers": {
    "stealth": {
      "command": "uvx",
      "args": ["invisible-playwright-mcp"]
    }
  }
}
```

**VS Code calls it `servers`**, in `.vscode/mcp.json` for a workspace:

```json
{
  "servers": {
    "stealth": {
      "type": "stdio",
      "command": "uvx",
      "args": ["invisible-playwright-mcp"]
    }
  }
}
```

**Codex uses TOML**, in `~/.codex/config.toml`:

```toml
[mcp_servers.stealth]
command = "uvx"
args = ["invisible-playwright-mcp"]
```

**Continue** uses YAML with its own block format, which changed recently enough
that we would rather point you at
[their documentation](https://docs.continue.dev/customize/deep-dives/mcp) than
print a block here that may already be stale.

### Where a proxy and the other settings go

Everything in **Settings** below goes under `env` on the server entry, in
whatever shape your client uses:

```json
{
  "mcpServers": {
    "stealth": {
      "command": "uvx",
      "args": ["invisible-playwright-mcp"],
      "env": {
        "STEALTHFOX_PROXY": "http://user:pass@proxy.example.com:8080",
        "STEALTHFOX_SEED": "4242"
      }
    }
  }
}
```

In Codex's TOML that is a `[mcp_servers.stealth.env]` table, and on the command
line Codex takes `--env KEY=value`. The Claude Code plugin and the Gemini CLI
extension carry a fixed config block, so their server reads these from the
environment the client was started in: export the variable, then start the
client.

⛔ **"Added" is not "connected".** Every one of these writes a config entry
without running anything, so a typo, a missing `uv`, or the first-run browser
download all surface later as a server that will not start. Check before you
trust it: `claude plugin list`, `codex mcp list`, `gemini extensions list`, or
your client's MCP panel.

## Settings

Environment variables, all optional. A proxy is the one worth adding: without it
the exit IP, timezone and locale are your own machine's, which is a real gap
between what the browser says it is and where it appears to be.

| Variable | Meaning |
|---|---|
| `STEALTHFOX_PROXY` | Proxy URL, e.g. `http://user:pass@proxy.example.com:8080` or `socks5://proxy.example.com:1080`. Host and port are both required. Bring your own. With it set, the session's timezone, locale and egress are derived from the proxy. |
| `STEALTHFOX_NO_PROXY` | `1` to go out from this machine's own address even when `STEALTHFOX_PROXY` is set. |
| `STEALTHFOX_SEED` | Integer seed for a deterministic fingerprint (same seed, same identity). A profile's own seed wins over this one. |
| `STEALTHFOX_PROFILE_DIR` | A directory for a persistent profile, so logins survive across runs. |
| `STEALTHFOX_BINARY` | Path to an engine binary you already have. It must be the build the packaged seal pins, or startup refuses. |
| `STEALTHFOX_HEADLESS` | `0` to run headed; headless by default. Decided by each launch: a saved session never records it, so a browser reopened by a headless server stays hidden even if it was last used headed. |
| `STEALTHFOX_MCP_TRANSPORT` | `http` to serve over streamable HTTP instead of stdio. Default is stdio, which is what MCP clients expect. What else changes when you flip it, including the one thing that changes silently: [local or remote](local-vs-remote-mcp-server.md). |
| `STEALTHFOX_MCP_HOST` | Bind address for the HTTP transport. Default `127.0.0.1`. |
| `STEALTHFOX_MCP_PORT` | Port for the HTTP transport. Default `8766`. It used to be `8765`, the invisible_playwright_mcp interface's own default, so running both meant a bind error with nothing to explain it. |
| `STEALTHFOX_OWNER_MODE` | `mcpd` enables isolated callers on trusted mcpd stdio. Unset preserves single-owner behavior. Other values and direct HTTP in owner mode are refused. |
| `STEALTHFOX_MAX_BROWSERS` | Owner mode only: positive integer, default `2`, across all callers, including launching and closing browsers. |
| `STEALTHFOX_OWNER_IDLE_SECONDS` | Owner mode only: positive finite seconds, default `900`, without tool activity before closing an owner's browsers. |
| `INVISIBLE_MCP_UPLOAD_DIRS` | Upload staging roots: absolute existing canonical directories, separated by `:` (`;` on Windows). Unset disables uploads. In owner mode the first root holds private owner directories; callers cannot upload directly from shared roots. |
| `INVISIBLE_MCP_DOWNLOAD_DIRS` | Download roots with the same path rules. Unset disables `browser_download`. In owner mode the first root holds private owner download directories; shared roots are not permitted destinations. |
| `INVISIBLE_MCP_HOME` | Where saved sessions are kept. Defaults to `%APPDATA%/invisible-playwright-mcp` on Windows, `~/Library/Application Support/invisible-playwright-mcp` on macOS and `$XDG_DATA_HOME/invisible-playwright-mcp` on Linux. A directory left by the previous name is moved onto this one the first time the command runs, once, and the move is printed. Set it to put them on another disk. |

Anything a tool call says wins over these. `browser_open` can pick another
seed, another exit or another profile for one browser; the variables are what
a browser gets when nobody says anything.

### Shared mcpd owner mode

This mode requires MCP SDK 1.30 or newer. It is for a **trusted stdio child of
mcpd**, not a directly exposed multi-user endpoint. mcpd must overwrite the whole
`params._meta["mcpd/identity"]` object on every `tools/call`, and must drop all
client-originated methods starting with `notifications/mcpd/` or `mcpd/`.
The child trusts that boundary; a party that can write its stdin directly is
the transport, not an authenticated end user.

The SDK request context supplies `_meta["mcpd/identity"]["sessionId"]`.
Missing, empty or malformed identities are refused, with no default owner.
Every tool, including list, status and watch, selects only that owner's
`main`/`support`. Each browser is a separate Firefox launch. Owner calls are
serialized, while different owners may run concurrently.

Profiles are newly created private directories (mode `0700`) inside a locked
process-instance directory and are removed after close. Saved sessions and `STEALTHFOX_PROFILE_DIR` are not read or written
in this mode. Supplying `profile` (even `""`) is refused; leave it out.
`browser_navigate` refuses `file:` URLs. Reopening creates a fresh profile:
the persistence descriptions elsewhere on this page apply to non-owner mode.

The global capacity includes reservations before launch and browsers still
closing. Slots are released only after successful close and profile removal.
A full process returns exactly:

```text
Browser capacity exhausted (N of N browsers in use across all sessions). Close one of yours with browser_close, or retry later. No browser was opened.
```

`N` is the configured limit. No other caller is evicted. A failed close retains
its slot and is reported rather than silently freeing capacity.

mcpd sends this notification when a caller ends:

```json
{"jsonrpc":"2.0","method":"notifications/mcpd/session_ended","params":{"sessionId":"the-ended-session","reason":"delete"}}
```

It closes only that owner; unknown sessions are ignored. The notification
adapter uses the SDK's decoded stdio stream and registered notification
handler because the SDK's standard client-notification union otherwise drops
extension methods. The idle backstop checks at most every 30 seconds; active
calls are not interrupted. Successful delegated calls also refresh the target's
activity. The last session's departure can stop the child without a
`session_ended` notification. Stdio EOF and, on Unix, SIGTERM close all owners
on the original event loop. The idle-task join is limited to 0.1 seconds and
graceful owner closes to three seconds. An independent daemon-thread watchdog
observes stdin hangup without consuming protocol bytes, and a synchronous
SIGTERM handler arms the same deadline. At 3.5 seconds it removes this process's
instances and forces exit code 1, even if engine process reaping is blocking the
event loop or executor shutdown. It stays armed until the event loop has exited.
This deliberately avoids closing loop-bound engine objects from another thread.
mcpd still terminates the browser process group within its five-second grace.
Failures are reported, never a silently successful close.

Forced termination (such as SIGKILL) cannot run cleanup. Before accepting calls,
owner mode sweeps `stealthfox-proc-*` directories directly under
`tempfile.gettempdir()` and the **first** upload and download roots. Each
process creates one `0700` instance under each distinct root and holds an
exclusive `flock` on its `.lock` file until cleanup or process death. A
`.stealthfox-instances-<uid>.lock` in each root serializes publication with sweeps.
Only same-UID instances whose lock can be acquired non-blocking are removed;
symlinks are never followed. A replacement child therefore cannot delete its
still-live predecessor's files, even if replacement initialization fails.

Legacy flat `stealthfox-owner-*`/`owner-*` directories and instances with no
verifiable lock are retained and counted in the log: their liveness cannot be
proved. Cleanup errors refuse startup. Owner mode requires POSIX descriptor
operations and locks; non-owner mode is unchanged.

Idle cleanup also removes empty owner records once no calls hold or wait for
their lock; a later call from the same session creates a fresh record. Ended
session tombstones retain only the latest 4096 identities, oldest evicted.
mcpd does not route calls for ended sessions, so this cache is defence in depth.

**Owner-scoped uploads.** On an owner's first browser launch, the server creates
`<first upload root>/stealthfox-proc-<instance>/owner-<32 random URL-safe characters>/`
with mode `0700`.
`browser_open` and ordinary `browser_status` report `upload dir: <path>`.
Copy documents into that directory before calling `browser_upload_files`.
Neither another owner's directory nor a file directly in a shared staging root
is allowed. Canonical path, hidden-component, size, descriptor and no-follow
checks still apply, both before touching the page and when snapshotting files.

The upload directory survives browser close/reopen, including helper browsers.
Session end, idle removal of the owner record, and process shutdown remove it
without following symlinks. A cleanup failure is reported and keeps the owner
record for retry. Handle-delegated status omits the upload directory; `main`'s
fill handle remains the final line of status.

Upload roots are validated at owner-mode startup and rechecked before directory
creation. Noncanonical, relative or missing configured roots are refused.
Owner mode also refuses startup if **any** upload root is equal to or an ancestor
of `tempfile.gettempdir()`, which contains browser profiles and upload snapshots.
Use a separate staging directory, never the whole temporary directory. With no
upload roots configured, browsing still works but uploads are off. These
owner-only restrictions do not change non-owner mode's shared-root behavior.

**Owner-scoped downloads.** On the first browser launch, the owner gets a
`0700` directory named `owner-<32 random URL-safe characters>` inside its
process instance under the first `INVISIBLE_MCP_DOWNLOAD_DIRS` root.
Open/status report `download dir: <path>`
before the final fill-handle line; handle-delegated status never reports it,
and `browser_download` is not a fill-handle tool.

Each browser has its own incoming-download subdirectory inside that owner
directory, configured through Firefox's download preferences. The tool polls
and reads only that owner's incoming files and saves only inside that owner's
download directory. `save_to` can select a non-hidden subdirectory there, not
another owner or a shared root. Directory descriptors and file descriptors
are checked against the owner boundary after page awaits as well as before the
click; directory links are refused. Missing destination components are
created with `mkdir(..., dir_fd=...)` and opened with `O_DIRECTORY|O_NOFOLLOW`
relative to the validated parent descriptor, so a swapped ancestor cannot
create directories in another owner's tree before the final check.

Closing/reopening a browser removes its temporary incoming files, not the
owner's saved files. Session end, idle removal, and process exit remove the
owner's entire download directory, including the exit fallback if browser
close fails. Startup sweeps stale owner download directories as described
above. Any configured download root containing the profile temporary
directory is refused. With no download roots configured, the tool is off;
spontaneous browser downloads remain inside that browser's private ephemeral
profile and are removed with it. Non-owner mode retains its shared configured
destinations and per-browser temporary incoming directories.

For credential filling, the owner's `browser_open` and `browser_status` return
`fill handle: bh_...` for **main only** (32 random bytes, 43 URL-safe base64
characters after the prefix). Pass this secret as `browser_handle` to the
trusted credential filler. It sends the handle on each tool call in
`params._meta["stealthfox/browser_handle"]`, together with its own mcpd identity.
This selects the target by capability, not by the filler's owner. It permits
only `browser_status`, `browser_evaluate`, `browser_snapshot`,
`browser_read_html`, `browser_read_text`, `browser_type`, and
`browser_press_key`. An explicit `browser="support"` is refused.

Handle-carrying calls validate the filler's identity without allocating an
owner record for that transport. With a valid handle, `browser_status` returns
the exact line `fill handle: <that handle>` as an attestation to the filler.
In non-owner mode `browser_status` never returns a `fill handle:` line, even
if a call supplies handle metadata.

Handles are compared in constant time after a hashed lookup, never logged,
and never echoed in errors. Close, reopen, expiry and session end revoke them.
There is no handle for `support`. Without this environment mode, metadata
does not change routing and no fill handles are issued.

## Tools

`browser_open`, `browser_close`, `browser_list`, `browser_status`,
`browser_navigate`, `browser_read_text`, `browser_snapshot`, `browser_read_html`,
`browser_take_screenshot`, `browser_watch`, `browser_click`, `browser_click_at`,
`browser_type`, `browser_select_option`, `browser_press_key`,
`browser_upload_files`, `browser_download`, `browser_evaluate`.

Tool names mirror the Microsoft Playwright MCP, so prompts written for it work
here too, with one deliberate departure: **there are no tab tools.** Three
groups: the two browsers, reading the page, and acting on it.

**Open first.** From 0.53.0 `browser_open` is the only tool that opens a
browser, and every other tool needs one that is open: if it is not, the tool
answers `the main browser is not open. Call browser_open to open it.` and
does nothing else. With no arguments `browser_open` brings back the person
this session already was - same seed, same exit, same profile - so a
conversation reopened tomorrow, or an assistant reconnecting, is the same
person without having to know the seed; pass a seed, a proxy or a profile
to be somebody else. Before 0.53.0 a command started a browser on your
behalf, a read refused, and a browser that had died was rebuilt underneath
the action and retried: three behaviours where one sentence does.

**A page that refuses does not cost the browser, and a browser that is gone
is said, not replaced.** A navigation that fails - a domain that does not
resolve, a page that times out - is reported as it happened, on the browser
you have, with its cookies and its pages intact. A browser that has actually
gone - the window closed by hand, the engine crashed - answers `the main
browser is gone: it closed or crashed. Call browser_open to open it again;
it comes back as the same person.` and is forgotten, so the next
`browser_open` starts clean. `browser_close` frees the engine and keeps who
the browser was, for the same reason.

If the tools do not appear in your client, the fastest way to tell a broken
registration from a broken server is to skip the client:
[a thirty-line MCP client](writing-an-mcp-client-in-python.md) lists them with
no model in the way.

This server exposes only tools, which is a choice rather than the only option:
the protocol also has resources and prompts, and
[who controls each](mcp-tools-resources-and-prompts.md) is why a browser fits
the first and not the other two.

**Every tool below also takes `browser`, optional, and it never appears in the
tables because the answer is the same for all of them.** Leave it out and you
get `main`, which is what a client that never mentions it has always got and
always will.

**In default, non-owner mode**, this server serves
exactly one - the two browsers below, and nothing else - so there is nothing
here to list, name, or reach a second one of: no tool takes an id for one, and
none can ask about one that is not its own. Which piece of work this is comes
from how the server was STARTED, never from a tool call. `uvx invisible-playwright-mcp` and a
checkout run directly always land on the same one; the invisible_playwright_mcp interface starts
a separate server **per conversation** and tells each which one it is the
moment it starts it, so two conversations are two processes with two saved
files, never one server juggling several behind your back.

A **session is one identity**, `main`: its page, its cookies, its fingerprint,
its logins. Beside it there may be ONE helper, `support`, for what must not
touch that identity: a temporary mailbox to receive a verification, a lookup, a
page you want to read without the site connecting it to the account. The two
share nothing. `browser` is a closed choice, `main` or `support` - there is no
name to invent - and the helper is not saved: it lives for the task and dies
with the process. Open it with `browser_open` when you need it and close it
with `browser_close` when you are done. By default it goes out through the
same exit as `main` and carries a fingerprint of its own.

⛔ **A browser drives ONE page, and there is no tool to open, list, choose or
close another.** `browser_navigate` opens the page and every other tool acts on
it. When you need a second page, that is what `support` is for - and it is the
better answer anyway: a second tab inside `main` would carry that identity's
cookies and fingerprint to the second site, which is the one thing the two
browsers exist to keep apart. A site can still open a page of its own; the
tools simply follow whichever page is live.

⛔ Until 0.39.0 a session could hold up to eight browsers under any names, and
`browser_focus` chose which one unaddressed commands meant. Until 0.41.0 every
tool also took a `session_id`, and one shared server juggled several sessions
behind it. All three are gone: a session is `main` plus `support`. A default server serves
one session for its whole life; trusted mcpd owner mode instead selects the
session from transport metadata, never from a tool argument.

### The two browsers

| Tool | Arguments | What it does |
|---|---|---|
| `browser_open` | `browser`, `seed`, `proxy`, `profile`, all optional | Opens `main` or `support`, or reopens one that is already up with those settings, which is how you change identity without changing which browser you are talking to. `support` left without a `proxy` goes out through `main`'s exit. |
| `browser_close` | `browser` optional | Closes `main` or `support` and frees what it held. Its page goes with it; the other browser is not touched. Who it was is kept: `browser_open` with no arguments brings the same person back. Close `support` when you are done with it. |
| `browser_status` | `browser` optional | Who is browsing right now: the seed, the exit, the profile and the page it is on. Starts nothing; a browser that is not open, or gone, is answered with the sentence that says which. |
| `browser_list` | none | Which of the two browsers are open, where each one is, and which one you are working in. **Answers JSON**: `focus`, the browser your last command acted in, or "" when none is open; `note`, which says how many are open and that a command naming no browser goes to `main`; and `browsers` with `id`, the `url` it is on and the `urls` of every page it holds. Only open browsers are listed, so every row is one you can act on. Starts nothing, so asking is free. |

`browser_open` is the first call of every session: nothing else opens a
browser. With no arguments it is the person this session already was, or a
fresh one the first time. Open `support` alongside it with `browser_open` and
address commands to whichever one you mean; `browser_open` called again on a
browser that is already up replaces it rather than adding a third, which is
why there are only ever two.

**The interface's conversation column and this server's saved identity are the
same idea, one layer up.** A conversation in `invisible-playwright-mcp ui` spawns its own server
and tells it, at the moment it starts, which conversation it is - never a tool
argument, because this server has no way to be asked about a second one. A
standalone client that names none of that, and a checkout run directly, both
land on the same place, `default`.

What is written down is who `main` is - seed, exit, profile - and nothing
else: `browser_open` writes it, `browser_open` with no arguments reads it
back, and no other tool touches the file. A conversation reopened tomorrow
is the same person the moment it opens the browser again; where the page
was is in the conversation, not in the file. Cookies and logins come back
only where a browser had a `profile`, which is the mechanism that already
exists for that. `support` is never written down: a helper that survived a
restart would be a second identity, which is the thing having only two fixed
roles rules out.

- **`seed`** is the identity. Same seed, same fingerprint, every time. Leave it
  out and one is drawn; the answer says which, so an identity worth repeating
  can be repeated.
- **`profile`** is a directory that keeps cookies and logins between opens.
  **A profile also owns its seed**: the first open on a new one stores the
  identity inside it and every later open reuses it, so a login never comes
  back wearing different hardware. Ask for a seed that contradicts the one a
  profile carries and you get a refusal naming both numbers, never a silent
  choice. A relative path is resolved against the server's own directory, and
  the answer reports the full path it used.
- **`proxy`** is where the traffic leaves, `http://user:pass@host:port` or
  `socks5://host:port`. Timezone, locale and geography follow it.
- Pass `""` for `profile` or `proxy` to insist on **none**, even when the
  environment sets a default. That is how you get an identity a site cannot
  link to another one.

**A profile does not own its exit the way it owns its seed.** The same login
arriving from another country is as visible as one arriving on different
hardware. You are warned when a profile's exit changes, but only when *you*
change it: a provider rotating its own addresses behind one host and port is
indistinguishable from here.

A `browser_open` that fails, usually because the proxy is down, leaves whatever
was already running untouched, and every later tool repeats the refusal until
`browser_open` succeeds. It does not quietly start a browser without the exit
that was asked for.

### Reading the page

| Tool | Arguments | What it returns |
|---|---|---|
| `browser_navigate` | `url`, `wait_until` | Goes to the url in the browser's page, opening one if none exists. Answers with the HTTP status and the url it landed on, so a 404 or a redirect to a login wall is visible instead of reading like a normal arrival. `wait_until` is `domcontentloaded` by default, which returns as soon as the markup is parsed; `load` waits for images and stylesheets, `networkidle` for a single-page app that fetches its content after load. |
| `browser_read_text` | `selector` (default `body`), `max_chars` (default 6000) | The visible text of an element, markup gone. The cheapest way to read a page. Long text is cut at `max_chars` and the cut is marked, so text without the marker is the whole thing. |
| `browser_snapshot` | `max_chars` | Title, url, and the interactive elements that are actually visible, each with a `selector` when one can reach it and `at: [x, y]`, its centre in viewport pixels. Not the accessibility tree: a single country `<select>` would contribute about two hundred `<option>` nodes and fill the cap before the form appears. |
| `browser_read_html` | `mode`: `form` (default), `text`, `full` | The page's HTML reduced to what is worth reading: `form` keeps the interactive surface and the text explaining it, `text` the prose alone, `full` the structure with the noise removed. Not capped, on purpose: cutting markup in the middle leaves tags that mean nothing, so on a large page the answer is long. |
| `browser_take_screenshot` | none | A screenshot of the page, as an image. |
| `browser_watch` | none | The whole browser window as a person at the machine sees it: tab strip, address bar, page and the pointer, from a live capture the session keeps running on the active tab. A capture that stops delivering, as it does when a headed window is minimised, is started again on the next look; if the window cannot be captured the tool says so rather than answering an old picture. |

The selectors a snapshot hands out are built to match exactly one element, and
that is the reason to pass them verbatim rather than writing your own: measured
across 958 elements on real pages, 88% could be addressed by a selector but only
48% unambiguously, and Playwright acts on the first match, so a caller aiming at
the third of five identical links would silently hit the first.

`browser_watch` is for the person watching, not for the model acting. The
pointer is drawn in the browser chrome on purpose, so that no page can see it,
which is also why no page screenshot can ever contain it. The picture is window
pixels: feed `browser_take_screenshot` to `browser_click_at`, not this. It needs
an engine from `firefox-28` on; an older engine answers with a sentence saying
so.

### Acting on the page

| Tool | Arguments | What it does |
|---|---|---|
| `browser_click` | `selector` | Clicks the first element matching a CSS selector, scrolling it into view and waiting for it to be clickable. The pointer approaches, hovers, presses and releases, the way a hand does. |
| `browser_click_at` | `x`, `y`, `hold_seconds` (default 0) | Clicks a viewport coordinate instead of a selector: moves the pointer there, presses, holds if asked, releases, and returns a screenshot taken right after. For a slider track, a canvas-drawn challenge, a precise point inside a wider element. |
| `browser_type` | `selector`, `text` | Fills a field, replacing whatever it holds. It sets the value rather than typing key by key, so per-keystroke handlers such as an autocomplete do not fire; for those, click the field and use `browser_press_key`. |
| `browser_select_option` | `selector`, `value` | Chooses an option in a `<select>`, by its visible label or by its value. |
| `browser_upload_files` | `selector`, `paths` | Attaches local files to a file input the way a person picks them: the input, or the button or label that opens its chooser, is clicked with the real pointer and the chooser is answered with `paths`. A hidden input is given the files directly, as its chooser would. Off unless `INVISIBLE_MCP_UPLOAD_DIRS` is configured. Files must be regular, non-hidden and at most 50 MB; owner mode permits only that owner's reported `upload dir`, while non-owner mode permits the configured roots. |
| `browser_download` | `selector`, or `x` and `y`; `timeout_seconds` (default 30); `save_to` | Clicks what makes the page hand over a file and saves that file: the browser's download or the document the click shows, read from the browser's response. A tab opened only to show the file is closed. Off unless `INVISIBLE_MCP_DOWNLOAD_DIRS` is configured. Owner mode saves only under the caller's reported `download dir`; non-owner mode permits the configured roots. `save_to` defaults to the first permitted directory, is created if missing, and nothing is overwritten. Answers JSON with `saved`, `filename`, `size`, `mime`, `sha256` and `url`. Each browser's incoming downloads are private and removed when it closes, rather than saved in `~/Downloads`. |
| `browser_press_key` | `key` | Presses a key on whatever has focus: `Enter`, `Tab`, `Escape`, `ArrowDown`, `Control+a`, or a single character. |
| `browser_evaluate` | `expression` | Runs JavaScript to **read** from the page and returns the result as JSON: a computed style, a value held in a framework's state, the length of a list. |

`browser_click_at` takes coordinates relative to the **viewport**, not to the
page, so the ones in a snapshot go stale the moment anything scrolls: a click, a
keypress, a lazy image loading above the fold. Nothing raises when that happens;
the click lands on whatever is at that spot now. Take a fresh snapshot after
anything that could have moved the page, and prefer `browser_click` with an
element's `selector` whenever it has one.

`browser_evaluate` reads; it will not act. Assigning to `value`, `checked` or
`selected`, or calling `click()`, `dispatchEvent()`, `submit()` or
`requestSubmit()`, is refused, and the refusal names the tool to use instead.
Script reaches the page with no keystroke and no pointer, so the event carries
`isTrusted` false, which is the clearest signal a page can collect that nobody
is really there. Reading those properties is fine. The refusal catches the
obvious spellings, not every possible one; a script that slips past it is still
the wrong way to do the thing.

### The order to try them in

The server hands every client this ladder, because a model that cannot find a
way down it invents one:

1. **A named tool with a selector**: `browser_click`, `browser_type`,
   `browser_select_option`, `browser_press_key`. `browser_snapshot` supplies the
   selector.
2. **Coordinates**: the snapshot reports `at: [x, y]` for every element, and
   `browser_click_at` moves the pointer there. For a canvas, a slider, a map, a
   widget built out of divs.
3. **A screenshot**: `browser_take_screenshot`, then `browser_click_at` on what
   you can see. For what the snapshot does not list at all.
4. **`browser_evaluate`**, to read what none of the above can see.

Getting to the bottom of the ladder without a way to do the thing is a result
too: a task reported as impossible is worth more than one completed in a way
that gets you blocked.

**Why each tool returns what it does**, with the measurements behind it:
[the tool design page](mcp-tool-design.md). **What each way of reading a page
actually costs**, measured on a small page and again on a long one where the
ordering reverses:
[text, HTML, snapshot or screenshot](what-should-the-agent-read.md).

## The second browser

Every tool above takes a `browser` argument, and the session holds two: `main`
and `support`. They are two identities rather than two windows, they share one
exit unless you give the second its own proxy, and a command that names neither
goes to `main` even when the focus is elsewhere.
[Two browsers in one session](two-browsers-in-one-session.md) has the measured
behaviour and the cases the second one is for.

## More than one client on the same browser

Over stdio the browser belongs to the client that opened it. Set
`STEALTHFOX_MCP_TRANSPORT=http` and it does not: the session is owned by the
server, so a second client can attach to the browser the first one left open,
and closing a client no longer kills the browser.

```bash
STEALTHFOX_MCP_TRANSPORT=http uvx invisible-playwright-mcp        # Linux
```
```powershell
$env:STEALTHFOX_MCP_TRANSPORT = "http"; uvx invisible-playwright-mcp   # Windows
```

To SEE the browser rather than share it, [invisible_playwright_mcp](https://github.com/feder-cr/invisible_playwright_mcp)
shows the live page beside the conversation.

## Notes

- This is a browser, not a captcha solver. It does not solve or bypass
  challenges for you; it makes an ordinary Firefox session look like a real one.
- Up to two browsers per server process, `main` and `support`, one page each.
  A third identity needs a second server.

## License

[MIT](https://github.com/feder-cr/invisible_playwright_mcp/blob/main/LICENSE),
the same as the engine it wraps.
