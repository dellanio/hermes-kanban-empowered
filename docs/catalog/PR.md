# plugin-catalog: add kanban-empowered

<!-- Draft of the catalog PR (title above, body below). Not submitted yet: replace PLACEHOLDER_SHA in
     kanban-empowered.yaml (sha and image) with the full 40-character commit being pinned. -->

Adds `plugin-catalog/kanban-empowered.yaml`, a community entry for
[dellanio/hermes-kanban-empowered](https://github.com/dellanio/hermes-kanban-empowered) (MIT), submitted
by the repository owner.

## What it does

Kanban Empowered makes the Hermes kanban work for a fleet of agent profiles with a person in the loop, so no
card stays silently stuck:

- **Human waits.** A worker that blocks a card on a person must write a structured ask (need, why, how to
  reply, optional options); a vague block goes back to the agent. The owner gets Telegram reminders
  (30 min, 2 h), an escalation (8 h) and a digest, and unblocks the card by replying to the item (`1 — done`,
  `3 — A`, free text), from the Hermes Desktop page, or with `hermes kanban-empowered done <card>`.
- **Stuck cards.** Cards held by Hermes' respawn guards are reported with a one-line hand-off command
  (`release <card> --to <profile>`).
- **Infrastructure failures.** When Hermes' failure breaker parks a card only because the host failed
  (gateway, worker persistence), the park is undone within a per-card budget; a burst raises one alert.
- **Visibility.** A pinned live panel on Telegram, `explain <card>`, `status [--watch]` and `doctor`, and a
  Hermes Desktop page (*Empowered*) with what needs you, what is running, the coordinator's timeline and
  infrastructure health.
- **Project status.** Boards can be marked Active, Suspended or Done (kept in the plugin's own store).

## Hermes surfaces used

- Hooks (`provides_hooks`): `on_kanban_dispatch_tick` (the coordinator), `on_kanban_worker_exited`
  (infrastructure-failure recorder), `pre_gateway_dispatch` (Telegram replies to the plugin's own messages;
  everything else continues to the normal gateway flow) and `pre_tool_call` (the structured-ask guard on
  `kanban_block`).
- `ctx.register_cli_command` (`hermes kanban-empowered ...`), `config_schema` (settings form),
  `provides_locales` (en, pt, pt-br language packs).
- Desktop plugin SDK only (`desktop/plugin.js`: a route, a sidebar item, a Kanban page-header button, a
  palette command) and a dashboard backend router (`dashboard/plugin_api.py`).
- Kanban reads and writes go through Hermes' kanban domain functions; the plugin never writes `kanban.db`.
- `hermes plugins validate` on a stock checkout at the pinned commit: passes (security scan `safe`, no
  core override, desktop surface clean); warnings for private core imports (below) and for the plugin's
  locale keys not being in the core catalog (its own namespace, by design).

## Disclosures (rule 13)

- **Network:** only the Telegram Bot API at `api.telegram.org` (`sendMessage`, `editMessageText`,
  `pinChatMessage`; never `getUpdates`, replies arrive through `pre_gateway_dispatch`). When that host is
  unreachable it uses Hermes' own IPv4 fallback path (`telegram_network`): IPv4 literals with Host/SNI kept,
  discovered with DNS-over-HTTPS lookups of `api.telegram.org` at `dns.google` and `cloudflare-dns.com`
  unless `TELEGRAM_FALLBACK_IPS` is set, through the proxy Hermes resolves. Nothing else; no telemetry.
- **Long-running work:** no extra process. The coordinator runs inside the gateway's kanban dispatch tick,
  in the coordinator profile only, behind a leader lock; short helper threads bound each Telegram call.
- **Reads outside its own data:** every live board's kanban database and board metadata, through Hermes'
  kanban functions; the Telegram bot token, only through `agent.secret_scope.get_secret` (on a multiplexed
  gateway's unbound dispatch tick, from Hermes' launch-profile body scope; a context serving another
  profile gets no token and one warning, never `os.environ`); for `doctor` only, each profile's
  `config.yaml` and the Hermes checkout's git `HEAD` files.
- **Writes:** card comments, unblocking a card waiting on a person when its owner answers, undoing an
  infrastructure park (within the budget) and reassigning a guard-held card on `release`, all through
  Hermes' kanban functions; its own SQLite store and leader lock under `plugin-data/kanban-empowered`;
  Telegram messages to the configured owner chats, with one pinned panel.
- **No shell commands, no subprocesses, no stored credentials, no self-update.**
- **Private core imports** (rule 9 warning, all in `ke/hermes_api.py`, the plugin's single adapter):
  `hermes_cli.kanban_db` / `kanban_db_boards` / `kanban_db_connect` (board and card reads and writes),
  `agent.i18n` / `agent.i18n_layers` (language and locale packs), `plugins.platforms.telegram.telegram_network`
  and `agent.ssl_verify` (Telegram fallback path and trust store), `tui_gateway.launch_profile_policy`
  (launch-profile secret scope inside the dispatch tick), `hermes_cli.profiles` (profile names and homes),
  `hermes_cli.plugins` (veto-hook detection), `hermes_yaml` and `hermes_cli` (`doctor`). I will open an issue
  asking for public seams for these (a documented kanban read/write API for plugins first).

## Optional core patch and the hook request

The repository ships `patches/0001-kanban-veto-hooks.patch`, which adds `kanban_pre_claim` and
`kanban_pre_complete` veto hooks to the kanban core. The plugin never applies it (applying it is a manual,
documented step) and works without it. It registers `kanban_pre_claim` (holding new claims on a board marked
Suspended or Done) only when the running Hermes exposes that hook: on a stock build nothing is registered,
and on a patched build `hermes plugins validate` reports `kanban_pre_claim` as an undeclared hook, by design,
since a stock build does not know it. Rather than list a patch, I will open an issue proposing these veto
hooks as a proper seam, with this plugin as the concrete consumer.

## Screenshots

Screenshots of the Desktop page (*Empowered*) will be added to this PR.
