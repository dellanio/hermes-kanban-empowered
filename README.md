<div align="center">

# 🗂️ Kanban Empowered

**A redesign for the Hermes kanban: clear asks, live status and recovery, so no card stays silently stuck.**

[![Hermes plugin](https://img.shields.io/badge/Hermes-Plugin-7F52FF.svg?style=flat)](https://github.com/NousResearch/hermes-agent)
[![Version](https://img.shields.io/badge/Version-0.3.1-blue.svg?style=flat)](plugin.yaml)
[![Python](https://img.shields.io/badge/Python-Standard%20library%20only-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org)
[![Telegram](https://img.shields.io/badge/Telegram-Two--way%20replies-26A5E4.svg?style=flat&logo=telegram&logoColor=white)](https://core.telegram.org/bots/api)
[![Languages](https://img.shields.io/badge/Languages-en%20%7C%20pt%20%7C%20pt--BR-green.svg?style=flat)](locales)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat)](LICENSE)

*Every board gets its own project status, a dashboard that explains what is happening, a live summary of what each agent is doing, and a one-line way to answer what the kanban needs from you, from Telegram or Desktop.*

![Kanban Empowered](docs/assets/banner.jpg)

</div>

---

## 💡 The Problem

Stop digging through card comments to find out what's going on. With a fleet of agent profiles on the Hermes kanban, a card can stop moving for many reasons: it waits on a person who never saw the question, a guard keeps it from respawning, or the host failed and Hermes parked it. Each of these looks the same from the board: a card that silently does not move.

---

## ⚡ The Solution: Kanban Empowered

**Kanban Empowered** gives every board its own project status, a dashboard that actually explains what's happening across your boards, a live plain-language summary of what each agent is doing right now, and a fast way out when the kanban needs you: a clear ask, answered in one line from Telegram or Desktop.

1. **Blocked?** It shows you exactly where the block starts and what it's holding back.
2. **Waiting on you?** A structured ask, reminders that escalate, and a one-line reply.
3. **Stuck for a technical reason?** It fixes what's safe to fix and tells you why about the rest.

**No card stays silently stuck.**

---

## 📸 Screenshots

Hermes Desktop page (*Empowered*). Captured from a live install; the sidebar is cropped, private details are blurred and the Portuguese UI text was redrawn in English, so some wording may differ slightly from the app.

<div align="center">
  <table>
    <tr>
      <td align="center"><b>See what each agent is doing right now</b></td>
      <td align="center"><b>Explain the block: where it starts and what it holds back</b></td>
    </tr>
    <tr>
      <td align="center">
        <img src="docs/assets/screenshots/06-current-task.jpg" alt="See what each agent is doing right now" width="420" />
      </td>
      <td align="center">
        <img src="docs/assets/screenshots/07-explain-block.jpg" alt="Explain the block: where it starts and what it holds back" width="420" />
      </td>
    </tr>
    <tr>
      <td align="center"><b>Overview: what needs you, what is running, the coordinator's timeline</b></td>
      <td align="center"><b>Infrastructure health</b></td>
    </tr>
    <tr>
      <td align="center">
        <img src="docs/assets/screenshots/01-overview.png" alt="Overview: what needs you, what is running, the coordinator's timeline" width="420" />
      </td>
      <td align="center">
        <img src="docs/assets/screenshots/01b-infrastructure.png" alt="Infrastructure health" width="420" />
      </td>
    </tr>
    <tr>
      <td align="center"><b>Detail of a card waiting on a person (reply box)</b></td>
      <td align="center"><b>Suspended projects</b></td>
    </tr>
    <tr>
      <td align="center">
        <img src="docs/assets/screenshots/02-detail.png" alt="Detail of a card waiting on a person (reply box)" width="420" />
      </td>
      <td align="center">
        <img src="docs/assets/screenshots/03-suspended.png" alt="Suspended projects" width="420" />
      </td>
    </tr>
    <tr>
      <td align="center"><b>Project status: Active / Suspended / Done</b></td>
      <td align="center"><b>The Empowered button on the native Kanban page</b></td>
    </tr>
    <tr>
      <td align="center">
        <img src="docs/assets/screenshots/04-projects.png" alt="Project status: Active / Suspended / Done" width="420" />
      </td>
      <td align="center">
        <img src="docs/assets/screenshots/05-kanban-button.png" alt="The Empowered button on the native Kanban page" width="420" />
      </td>
    </tr>
  </table>
</div>

---

## ✨ Features at a glance

| Area | What you get |
|---|---|
| **One coordinator** | A single process (leader lock) watches every board on Hermes' dispatch tick and owns one shared state, so a fleet of profiles never sends the same reminder twice. |
| **Clear asks** | A worker that blocks a card on a person must write a structured ask (need, why, how to reply, optional options); a vague block is sent back to the agent to redo. |
| **Reminders that escalate** | 30 min → 2 h → escalation at 8 h → daily digest, with no quiet hours. Ask-less cards are worded from their title, never from technical markers. |
| **Telegram, done properly** | Branded `[Kanban - Empowered]` messages, action-first layout, numbered items, replies tied to the item (`1 — feito`, `2 — A`, free text), a pinned live panel kept up to date, and a transport that survives a flaky route to `api.telegram.org` (IPv4 fallback with SNI, sticky last-good path, bounded time). |
| **Stuck-card recovery** | Respawn-guard holds reported with a one-line hand-off command; host-only failures parked by Hermes' breaker undone within a budget; a burst of infrastructure exits raises one systemic alert. |
| **Explain everything** | `explain <card>` lists the conditions that keep a card from moving; `status` / `status --watch` show the fleet; `doctor` checks the whole setup. |
| **Empowered dashboard** | A Hermes Desktop page (sidebar menu and Kanban header button): counters, *Needs you* sorted by impact, *Running now*, the coordinator's timeline, infrastructure health, and a drawer to read the ask and **answer it from the desktop**. |
| **Project status** | Mark a board Active, Suspended or Done: suspended projects leave the dashboard's main list, stop getting reminders and have new claims held (via the core veto hook). |
| **Settings screen** | Every setting editable from Hermes Desktop (Capabilities / Settings > Plugins). |
| **Languages** | English, `pt` and `pt-br` catalogs (parity-tested); follows the Hermes language or a plugin-only override. |
| **Plays nice** | Coexists with Hermes' own notifier and never repeats its native messages. |

Details:

- **Human waits (Telegram).** A card blocked on a person gets a reminder after 30 min and 2 h, an
  escalation after 8 h and a daily digest after that. The owner answers the reminder on Telegram,
  naming the item (`1 — conectei`, `3 — A`, or free text), or runs
  `hermes kanban-empowered done <card>`, and the card is unblocked.
  When the reminder layout changes in a new plugin version, every open wait is sent once more in
  the new layout (its stage is not advanced). A wait that holds other cards can get its digest on
  its own cadence (`waits.blocking_digest_every`), and when a wait reaches the escalation with no
  `escalate_to` configured, the owner gets one setup alert saying so.
- **Stuck cards.** Cards held by Hermes' respawn guard (`active_pr`, `blocker_auth`) for more than
  30 min are reported to the owner, again every `waits.digest` while the guard lasts, and listed in
  *Needs you* with the reason and the last error, with a one-line command to hand them to another profile.
- **Infrastructure failures.** When Hermes' failure breaker parks a card only because the host
  failed (gateway, worker persistence), the plugin undoes the park, within a budget of 2 undos per
  card per 24 h. A burst of 3 infrastructure exits within 1 h raises one systemic alert.
- **Visibility.** `explain` says why a card is (not) moving, `status` lists the fleet, and a pinned
  Telegram message (the live panel) is kept up to date. `status --watch` shows the same panel in
  the terminal.

The plugin coexists with Hermes' own notifier: it never repeats the native completed, blocked,
gave_up, crashed, review or block_loop_detected messages.

**Status:** v0.3.1.

---

## 🛠️ Architecture

```mermaid
flowchart TD
    subgraph Workers ["Every worker profile"]
        Guard["Worker guard (pre_tool_call)"] -->|"structured ask on kanban_block"| Board[("Hermes kanban boards")]
    end

    subgraph Coord ["Coordinator profile (gateway)"]
        Tick["on_kanban_dispatch_tick"] --> Coordinator["Coordinator (leader lock)"]
        Exit["on_kanban_worker_exited"] --> Coordinator
        Coordinator --> Store[("Plugin state (SQLite)")]
        Coordinator -->|"comments, unblock, undo, release"| Board
        Inbound["pre_gateway_dispatch"] -->|"owner replies"| Coordinator
    end

    subgraph Desktop ["Hermes Desktop"]
        Page["Empowered page (desktop/plugin.js)"] <--> Backend["Backend (dashboard/plugin_api.py)"]
    end

    Coordinator -->|"reminders, pinned panel"| Telegram["Telegram Bot API"]
    Telegram -.->|"replies"| Inbound
    Backend --> Store
    Backend --> Board
    Backend -->|"running-card summary"| Aux["Hermes auxiliary model"]
```

- **Runtime:** inside the Hermes gateway; no extra process or daemon.
- **Language:** Python, standard library only.
- **State:** the plugin's own SQLite store and leader lock under `plugin-data/kanban-empowered`.
- **Kanban access:** Hermes' kanban domain functions only; the plugin never writes `kanban.db`.
- **UI:** Hermes Desktop plugin SDK, plus a settings form generated from `plugin.yaml`.

---

## 📋 Requirements

- Hermes Agent at revision `bf0203a794` (the revision this version was written and tested
  against; `doctor` prints both `hermes_compat` and the installed `hermes_head`), with the kanban
  enabled.
- Optional: the core patch [`patches/0001-kanban-veto-hooks.patch`](patches/README.md). It adds the
  `kanban_pre_claim` and `kanban_pre_complete` veto hooks. The plugin uses only `kanban_pre_claim`, to hold
  new claims on a Suspended or Done project (see "Project status"); everything else behaves the same
  with or without the patch. Without it, `doctor` reports `veto_hooks: missing` and the coordinator
  warns once (log and Telegram).
- Python standard library only (no extra dependencies).

---

## 🔒 Disclosures

What the plugin does beyond its own data, for review before installing:

- **Network.** Only the Telegram Bot API at `api.telegram.org` (`sendMessage`, `editMessageText`,
  `pinChatMessage`; it never polls the bot or calls `getUpdates`). When that host is unreachable it uses
  Hermes' own IPv4 fallback path (`plugins.platforms.telegram.telegram_network`), as Hermes' Telegram
  adapter does: IPv4 literals of `api.telegram.org` with Host/SNI kept, discovered (unless
  `TELEGRAM_FALLBACK_IPS` is set) with DNS-over-HTTPS lookups of `api.telegram.org` at `dns.google` and
  `cloudflare-dns.com`, and the proxy Hermes resolves (`TELEGRAM_PROXY` / system proxy). The "Current task"
  line also calls the Hermes auxiliary model (see "Running cards" below), through Hermes' own client and the
  provider you configured. Nothing else; no telemetry or usage reporting.
- **Long-running work.** No extra process and no daemon. The coordinator runs inside the gateway's kanban
  dispatch tick (`on_kanban_dispatch_tick`, evaluating at most every 30 s), in the coordinator profile only,
  behind a leader lock in `shared_state_dir`; short-lived helper threads bound each Telegram call, DNS lookup and running-card summary.
- **Reads outside its own data.** Every live board's kanban database and board metadata, through Hermes'
  kanban functions (read-only except the writes below); the Telegram bot token, through Hermes' secret scope
  (`agent.secret_scope.get_secret`; see "Configuration"); for `doctor` only, each profile's `config.yaml`
  (is the plugin enabled there) and the Hermes checkout's git `HEAD` files (installed revision). The state
  store lives under the coordinator profile and is read by the plugin in every other profile.
- **Telegram replies.** Replies from the configured owners to the plugin's own messages are consumed by the
  plugin before they reach the agent (`pre_gateway_dispatch`). Everything else passes through untouched.
- **Running cards.** To show what a running card is doing, the Desktop backend reads the worker's recent
  session from its profile's `state.db`, read-only: what the agent wrote, including its reasoning, and the
  tool names; never commands or tool output. It redacts secrets (`agent.redact`) and sends that to the Hermes
  auxiliary model (`agent.auxiliary_client`; `auxiliary.kanban_empowered` in `config.yaml`, else auto), only
  when the session changed and at most once a minute per card. This consumes tokens from that provider.
  Redaction is pattern-based, so a secret an agent wrote in an unusual form could reach the provider.
- **Writes.** On kanban cards, only through Hermes' kanban domain functions, never by writing `kanban.db`:
  comments, unblocking a card waiting on a person when its owner answers (Telegram, Desktop or the `done`
  command), undoing a park caused by an infrastructure failure (within the per-card budget), and reassigning
  a guard-held card when you run `release`. Its own SQLite store and leader lock under
  `plugin-data/kanban-empowered` (`shared_state_dir`). Telegram messages to the configured owner chats only,
  with one pinned live panel in the default owner's chat.
- **No shell commands**, no subprocesses, no stored credentials (it never writes or caches the token).
- **Private core imports** (all in `ke/hermes_api.py`, the plugin's only adapter to Hermes; `hermes plugins
  validate` lists them as a rule 9 warning): `hermes_cli.kanban_db`, `kanban_db_boards` and
  `kanban_db_connect` (board and card reads and writes), `agent.i18n` and `agent.i18n_layers` (the Hermes
  language and the plugin's locale packs), `plugins.platforms.telegram.telegram_network` and
  `agent.ssl_verify` (the Telegram fallback path and its trust store), `agent.auxiliary_client` and
  `agent.redact` (the running-card summary), `tui_gateway.launch_profile_policy`
  (the launch profile's secret scope inside the gateway tick), `hermes_cli.profiles` (profile names and
  homes), `hermes_cli.plugins` (veto-hook detection), `hermes_yaml` (reading `config.yaml` for `doctor`) and
  `hermes_cli` itself (locating the checkout for `doctor`). A seam request for these will be filed upstream.
- **Optional core patch.** [`patches/0001-kanban-veto-hooks.patch`](patches/README.md) is never applied by
  the plugin; applying it is a manual step. The plugin registers `kanban_pre_claim` only when the running
  Hermes exposes that hook, so on a stock build it is not registered, and on a patched build `hermes plugins
  validate` reports it as an undeclared hook, by design (it is not in `provides_hooks` because a stock build
  does not know it).

---

## 🚀 Install

Step-by-step guide with verification and rollback: [`docs/INSTALL.md`](docs/INSTALL.md). Live acceptance results of the first release (v0.1): [`docs/ACCEPTANCE-v0.1.md`](docs/ACCEPTANCE-v0.1.md).

Plugins, their state and their settings are per Hermes profile. Install and enable the plugin in
**every profile that runs kanban workers** (builder, devops, reviewer, ...) and in the profile of
the gateway that acts as the coordinator (by default `default`).

For each profile:

1. Put this repository in the profile's plugin directory, `<profile home>/plugins/kanban-empowered/`
   (`~/.hermes/plugins/kanban-empowered/` for `default`,
   `~/.hermes/profiles/<name>/plugins/kanban-empowered/` for a named profile).
   `hermes plugins install <git url>` does the same.
2. Enable it: `hermes plugins enable kanban-empowered` (it goes into `plugins.enabled`).
3. Add the settings below to that profile's `config.yaml`. Every profile must agree on
   `coordinator_profile` and `shared_state_dir` (the default `shared_state_dir` is derived from
   `coordinator_profile`, so leaving both at their defaults everywhere is consistent).
4. Run `hermes kanban-empowered doctor` in that profile.

What runs where:

- **Every profile** registers the worker guard (`pre_tool_call`): a `kanban_block` that waits on a
  person must carry a structured ask (`Need:` / `Why:` / `How to reply:`, or the pt-BR labels
  `Preciso:` / `Por quê:` / `Como responder:`), otherwise the agent is asked to redo the block.
  Optional lines: `Subject:` / `Assunto:`, `Next step:` / `Próximo passo:` and `Options:` /
  `Opções:` followed by one `A — choice` line per option.
  Worker profiles open no plugin state.
- **The coordinator profile** (only) opens the shared state, evaluates the boards on Hermes'
  dispatch tick (at most every 30 s), sends Telegram messages, reads the Telegram replies and
  records worker exits. A leader lock in `shared_state_dir` makes sure only one coordinator
  process acts at a time; a second one stays passive.

---

## ⚙️ Configuration

Settings live under `plugins.entries.kanban-empowered.settings`:

```yaml
plugins:
  entries:
    kanban-empowered:
      settings:
        mode: enforce                 # enforce | cooperative-only
        # language: pt                # plugin messages' language; default: follow Hermes
        coordinator_profile: default  # the profile whose gateway is the coordinator
        # shared_state_dir: C:/Users/me/AppData/Local/hermes/plugin-data/kanban-empowered
        telegram:
          owner_ids: { sponsor: 6802889854, ops: 5550001 }
          pin_live_panel: true
          reply_ttl: 7d
        waits:
          default_owner: sponsor
          reminders: [30m, 2h]
          escalate_after: 8h
          escalate_to: ops
          digest: daily
          human_kinds: [needs_input, capability]
          require_structured_ask: true
        boards:
          side-project:
            waits: { default_owner: ops }
        guards:
          alert_after: 30m
        infra_failures:
          undo_budget: { count: 2, window: 24h }
          alert_count: 3
          alert_window: 1h
          # patterns: ["Kanban execution unknown", "retained by owner",
          #            "Gateway connection failed", "WorkerPersistenceError"]
```

| Key | Default | Meaning |
|---|---|---|
| `mode` | `enforce` | `enforce` or `cooperative-only`. Validated, but without effect for now (reserved for the veto hooks). |
| `language` | none | Language of the plugin's messages (`pt`, `en`, ...). None follows the Hermes language. |
| `coordinator_profile` | `default` | The profile whose process acts as the coordinator. |
| `shared_state_dir` | the coordinator profile's `plugin-data/kanban-empowered` | The plugin's SQLite state and leader lock, shared by all profiles. Default: `<hermes root>/plugin-data/kanban-empowered` when `coordinator_profile` is `default`, else `<hermes root>/profiles/<coordinator_profile>/plugin-data/kanban-empowered`. **Must be on a local NTFS (or other local) disk**: the leader lock relies on hard links and atomic renames, which network shares and some sync folders do not provide. |
| `telegram.owner_ids` | `{}` | Owner name to Telegram id. **Each value is BOTH the person's Telegram user id and the id of their private chat with the bot** (in a Telegram DM they are the same number): messages go to that chat, and a reply is accepted only from that user. |
| `telegram.pin_live_panel` | `true` | Keep a pinned live panel in the default owner's chat. |
| `telegram.reply_ttl` | `7d` | How long a reminder can still be answered. |
| `waits.default_owner` | `sponsor` | Owner of the waits on any board without an override. |
| `boards.<board>.waits.default_owner` | | Per-board owner. |
| `waits.reminders` | `[30m, 2h]` | Reminder times, counted from the start of the block. |
| `waits.escalate_after` | `8h` | When the escalation goes out (to the owner, plus a copy to `escalate_to`). |
| `waits.escalate_to` | none | Owner name that receives a read-only copy of escalations. It cannot resolve them. |
| `waits.digest` | `daily` | Repeat interval after the escalation (`hourly`, `daily`, `weekly` or a duration). |
| `waits.blocking_digest_every` | same as `waits.digest` | Digest interval for waits that hold other cards back (`hourly`, `daily`, `weekly` or a duration like `6h`). Empty = the same as `waits.digest`. |
| `waits.human_kinds` | `[needs_input, capability]` | Block kinds that wait on a person (an untyped block counts too). |
| `waits.require_structured_ask` | `true` | The worker guard requires the three-line ask. |
| `guards.alert_after` | `30m` | How long a respawn guard may hold a card before the owner is told (repeated every `waits.digest`). |
| `infra_failures.undo_budget` | `{count: 2, window: 24h}` | Breaker parks undone per card per window. |
| `infra_failures.alert_count` / `alert_window` | `3` / `1h` | Systemic alert threshold. |
| `infra_failures.patterns` | see above | Error substrings that mark a worker failure as the host's. |

Durations accept `s`, `m`, `h`, `d` (`30m`, `2h`, `7d`) or plain seconds.

The Telegram bot token is `TELEGRAM_BOT_TOKEN` of the coordinator profile (the same bot the gateway
already uses), read only through Hermes' secret scope (`agent.secret_scope.get_secret`), never from a bare
`os.environ` read or a file. On a multi-profile (multiplexed) gateway the dispatch tick has no profile bound;
there the plugin takes the launch profile's own value from Hermes' launch-profile scope, and in a context
serving another profile it gets no token (one warning) rather than borrowing one. The plugin only calls
`sendMessage`, `editMessageText` and `pinChatMessage`; it never polls the bot.

---

## 🎛️ Settings screen

In Hermes Desktop the plugin has a settings form (Capabilities, plugin gear, or Settings > Plugins),
generated from the `config_schema` in [`plugin.yaml`](plugin.yaml). It edits the common keys:

| Form field | Same as in `config.yaml` |
|---|---|
| Message language | `language` |
| Mode | `mode` |
| Owner Telegram id | `telegram.owner_ids.<default owner>` |
| Default owner | `waits.default_owner` |
| Reminders (`30m,2h`) | `waits.reminders` |
| Escalate after | `waits.escalate_after` |
| Digest every | `waits.digest` |
| Digest every (waits that block cards) | `waits.blocking_digest_every` |
| Escalate to | `waits.escalate_to` |
| Require structured ask | `waits.require_structured_ask` |
| Pin live panel | `telegram.pin_live_panel` |
| Stuck-card alert after | `guards.alert_after` |
| Infra undo budget / window | `infra_failures.undo_budget.count` / `.window` |
| Show the sidebar menu | `show_sidebar_menu` (Desktop dashboard, see below) |
| Show the Kanban button | `show_kanban_button` (Desktop dashboard, see below) |

The form stores each value as a flat key (`plugins.entries.kanban-empowered.settings.default_owner`,
not `...settings.waits.default_owner`), because that is how Hermes Desktop reads and writes plugin
settings. The plugin folds those flat keys onto the nested ones, with this precedence:

1. A non-blank flat key (what the form wrote) wins over the nested key it stands for.
2. A blank flat value (`""`) counts as unset and falls through to the nested key, then the default.
3. `telegram_owner_id` becomes `owner_ids: {<default owner>: <id>}` only when `telegram.owner_ids`
   is not set; an explicit `owner_ids` mapping always wins.

Hand-written nested config keeps working unchanged. When a form value differs from a nested
`config.yaml` value it shadows, the plugin logs one warning per load and `hermes kanban-empowered doctor`
prints a `config_conflict:` row for each; a `telegram_owner_id` that an explicit `owner_ids` leaves
unused (no id there for the default owner, or a different id) is reported the same way.

Limits of the form:

- It shows the schema default for a field until that field is saved, not the value that is effective
  from `config.yaml`. Until the form writes the field, the nested value (or the built-in default) applies.
- Booleans and numbers cannot be unset from the form: once saved, `Require structured ask`, `Pin live
  panel` and `Infra undo budget` always hold a value. To go back to the nested or default value, delete
  the flat key from `config.yaml`.
- `Owner Telegram id` cannot be cleared from the form (an empty number is rejected). Remove
  `telegram_owner_id` from `config.yaml` to drop it.
- A blank `Reminders` field means the `waits.reminders` value from `config.yaml`, or the default `30m,2h` when
  there is none. "No reminders" cannot be set from the form.
- `Escalate to` only names a contact; that contact needs a chat id in `telegram.owner_ids`, which can
  only be edited in `config.yaml` (the form has no mapping field). Without it, escalation copies are not sent
  and the coordinator warns once.

Keys the form cannot express stay in `config.yaml`: `coordinator_profile`,
`shared_state_dir`, `telegram.owner_ids` (several owners), `telegram.reply_ttl`, `boards`,
`waits.human_kinds` and `infra_failures.alert_count` / `alert_window` / `patterns`.

[`docs/catalog/kanban-empowered.yaml`](docs/catalog/kanban-empowered.yaml) is the draft entry for the
Hermes plugin catalog (the card image comes from there, not from `plugin.yaml`).

---

## 🖥️ Desktop dashboard (Empowered)

The package also ships a Hermes Desktop half (`desktop/plugin.js`) and its backend
(`dashboard/plugin_api.py`, mounted by Hermes at `/api/plugins/kanban-empowered/`). Two switches,
both off until you turn them on, as for every Desktop plugin: the agent plugin enabled in the
profile (`plugins.enabled`, which also mounts the backend; restart the gateway after enabling) and
the Desktop half enabled in **Capabilities > Plugins**.

- **Kanban-Empowered** in the sidebar (setting *Show the sidebar menu*) opens the page; a compact
  **Empowered** button appears in the header of the Kanban page (setting *Show the Kanban button*;
  needs the bundled Kanban Desktop plugin enabled and a Desktop that exports the page-header area).
  The palette command *Kanban Empowered* opens it too.
- The page shows four counters (need you, impacted cards, active workers, incidents), **Needs you**
  (sorted by cards held behind, then age; infrastructure cards whose undo budget is used up in red),
  **Running now**, **Coordinator at work** (unblocks via Telegram/desktop/CLI, released guards,
  undone infrastructure failures, reminders sent, panel updates) and **Infrastructure** (failures in
  the last hour, systemic alert, Telegram path, cards out of retries). It refreshes every 10 s and
  can be filtered by board.
- **View details** opens a drawer with the project, the request exactly as the Telegram reminder
  words it, the options, the explain conditions and the last comments. **Send** answers it the same
  way a Telegram reply would (an option letter or a confirmation word resolves; a "not yet" only adds
  a comment; any other text is the answer and is posted on the card) and the card comment says
  "via Kanban-Empowered (desktop)". The answer is applied only while the card is still blocked on the
  block you were looking at.
  Only a card waiting on a person can be answered (as on Telegram); a card parked by Hermes'
  failure breaker is shown read-only.
- Any authenticated Hermes Desktop or web-dashboard session against this gateway can answer the
  waits of any board (Telegram checks the owner of each board; the dashboard does not), and the answer
  is recorded as "desktop" without a user name.

Texts follow the plugin language (`language`, else Hermes'); the product name is not translated.

### Project status (Active / Suspended / Done)

Hermes boards have no lifecycle status, so the plugin keeps one per board in its own state
(`state.db`; `board.json` is never touched). A board without a status is **Active**. Set it from
the **Projects** button of the dashboard or with `hermes kanban-empowered board-status`.

A **Suspended** or **Done** board:

- leaves **Needs you** and the four counters; its items go to the collapsed **Suspended projects (N)**
  section, tagged with the status, and can still be opened and answered. A reply to an older Telegram
  reminder of a now-suspended board still resolves it too (same version checks as any reply);
- gets no Telegram reminders, escalations or digests for its waits, and the pinned panel leaves it out
  (one line "N suspended projects" at the end). Infrastructure alerts are unaffected;
- has its new claims held through the core veto hook `kanban_pre_claim` (the card stays *ready*, with a
  `claim_refused` event "Project suspended in Kanban Empowered — reactivate it to continue."); workers
  already running finish normally. Without the veto hooks patch (`doctor`: `veto_hooks: missing`) claims
  are not held; the rest still applies. **Review-lane claims are not held yet**: the dispatcher picks up a
  card in *review* through `claim_review_task`, which the current core patch does not hook, so a reviewer
  can still start on such a board; this needs a core patch update (`doctor` row `boards_hold_scope`).

`hermes kanban-empowered explain <card>` shows "start held: project suspended" for a ready card of such a
board. While the only ready cards are on suspended/done boards, the gateway may log its "kanban dispatcher
stuck" warning every few minutes (nothing was spawned although cards are ready); it is expected and harmless.

Back to **Active**, claims flow again and the open waits restart their reminder ladder from that moment
(first reminder one first stage later, no burst of the stages missed meanwhile).

---

## 💻 Commands

| Command | What it does |
|---|---|
| `hermes kanban-empowered doctor` | Compatibility and setup diagnostics (see below). |
| `hermes kanban-empowered explain <card> [--board B]` | Why a card is (not) moving: dependencies, guard, live run, freshness, human wait. |
| `hermes kanban-empowered status [--watch]` | Running cards, human waits, guarded cards, resolved today. `--watch` redraws the live panel every 5 s until Ctrl+C. |
| `hermes kanban-empowered done <card> [--board B]` | Resolve a card's open human wait, exactly like a `done` reply on Telegram (same version checks). |
| `hermes kanban-empowered release <card> --to <profile> [--board B]` | Hand a card held by a respawn guard (`active_pr`, `blocker_auth`) to another profile. |
| `hermes kanban-empowered board-status [<board> [active\|suspended\|done]]` | Without arguments, every board with its project status; with a board, its status; with a status (also `ativo`, `suspenso`, `concluído`, accents optional), set it. |

### Telegram replies

Reply (with Telegram's "reply" function) to a reminder or to the pinned panel and name the item:
`1 — conectei`, `2 — o campo ficou pequeno`, `3 — A` (also `1 – …`, `1 - …`, `1: …`, `1) …`,
`1. …`, `1 …`). A reminder with a single item also takes the bare text; the pinned panel only takes
numbered replies (anything else is a normal message for the agent). The text after the number:

- a letter the item lists as an option (`3 — A`) picks that option and unblocks the card (the choice
  is commented); a letter it does not list, or a bare `ok` on an item with options, changes nothing
  and the plugin asks for a listed letter;
- a confirmation word unblocks the card: the item's own reply words (from its `How to reply:`) or
  `feito`, `conectei`, `validado`, `liguei`, `ok`, `aprovado`, `pronto`, `done`, `connected`,
  `validated`, `approved` (any case, trailing punctuation/emoji allowed);
- an explicit "not done" (`não`, `ainda não`, `negativo`, `not yet`, `not`, `nope`, `never`, and `no`:
  at the start in English, only on its own in Portuguese) only adds a comment; the card keeps waiting;
- any other text is your answer: the card is unblocked and the text is posted as a comment, so the
  agent continues with it;
- several items and no number: the plugin asks which item.

A reply is acted on only when it comes from the configured owner of that item, in the owner's own
chat, to a message the plugin sent, before `reply_ttl`, and while the card is still blocked on the
same block it was reminded about. If the card was blocked again (a new block, including a park by
Hermes' failure breaker), the reply changes nothing and the plugin says so. Replies from anybody
else are ignored and continue to the normal gateway flow. Every resolution becomes a card comment
with its author.

### `doctor` output

| Key | Value |
|---|---|
| `hermes_compat` | Hermes revision this plugin version targets. |
| `hermes_head` | Installed Hermes revision (`unknown` if it cannot be read). |
| `veto_hooks` | `present` / `missing` (the core patch; the plugin uses `kanban_pre_claim` to hold claims on suspended/done projects). |
| `coordinator` | PID of the process holding the leader lock, or `none`. |
| `active_profile` | Profile of the process running `doctor`. |
| `coordinator_profile` | Configured coordinator profile. |
| `shared_state_dir` | Resolved state directory. |
| `profiles_with_plugin` | Profiles where the plugin directory exists and `config.yaml` enables it (`plugins.enabled`, not in `plugins.disabled`) or has a `plugins.entries.kanban-empowered` section. |
| `telegram_token` | `present` / `missing`: whether Hermes' secret scope resolves `TELEGRAM_BOT_TOKEN` in **this** process (the token itself is never printed). |
| `owner_chats` | Number of entries in `telegram.owner_ids`. |
| `boards_suspended` | Boards whose project status is Suspended or Done (`slug`, `slug (Done)`), or `none`. Read-only. |
| `boards_hold_scope` | What a Suspended/Done board holds: new claims, except review-lane claims (not hooked by the current core patch; needs a core patch update), or nothing when the veto hooks are missing. |
| `telegram_path` | Only once a Telegram client has recorded one: how the Bot API was reached last, `<host> (hostname)` or `<ip> (IPv4 fallback)`. |
| `config_conflict` | One row per settings-form value that shadows a different `config.yaml` value, or a `telegram_owner_id` that `telegram.owner_ids` leaves unused. |
| `config` | Only when the settings are invalid: the error. The plugin then runs the worker guard on defaults and does not start the coordinator. |

Human-facing text follows the Hermes language (English by default; `pt` and `pt-br` included).
The optional top-level setting `language` (e.g. `language: pt`) overrides it for this plugin's
messages only. Telegram messages never show card ids, hashes or role names; the CLI keeps the ids.

---

## 🚧 What it does not do yet

- The veto hooks are used only to hold claims on Suspended/Done projects. There is no refusal at
  completion time, no resource locks or quota groups (`quota`, `held` commands), no gate verdicts
  or four-eyes review.
- Review-lane claims on a Suspended/Done project are not held yet (needs a core patch update, see
  "Project status").
- No agent tools (`kanban_true_state`, `kanban_verdict`); the only agent-facing check is the
  structured ask on `kanban_block`.
- Breaker parks are not tracked as human waits (no reminders for them): the infrastructure
  recovery and its alerts handle them, and Hermes' own notifier announces the `gave_up`.
- No quiet hours. Reminders follow the wall clock; while the gateway is down they wait, then go
  out condensed into one message per owner.
- Telegram is the only messaging channel; the Desktop dashboard is the other way to answer.
- The Desktop dashboard does not check per-board owners (see "Desktop dashboard").

---

## 📄 License

This project is open-source under the MIT License. See [LICENSE](LICENSE) for details.
