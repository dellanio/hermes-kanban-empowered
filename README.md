# kanban-empowered

Plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that
extends the native kanban: reminders and escalation for cards waiting on a
human (via Telegram), guards for stuck cards, and recovery from
infrastructure failures.

**Status:** v0.1 em desenvolvimento.

## Requirements

- Hermes Agent with the kanban enabled.
- The Hermes veto-hooks patch in `patches/` (adds the `pre_gateway_dispatch`
  and kanban hooks the plugin relies on). Without it the plugin degrades to
  the current Hermes behaviour (fail-open) and `doctor` reports
  `veto_hooks: ausentes`.

## License

MIT, see `LICENSE`.
