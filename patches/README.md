# Hermes core patches

`kanban-empowered` is a plugin, but two of its guarantees need a hook that the Hermes
core does not ship yet. The patches in this directory add them. The plugin runs without
them (it reports the missing hooks in `doctor`), but the veto behaviour stays inactive
until they are applied. v0.1 of the plugin does not consume the hooks yet.

## 0001-kanban-veto-hooks.patch

Adds two veto hooks, fired BEFORE the board write:

| Hook | Fired from | Kwargs | Return |
|---|---|---|---|
| `kanban_pre_claim` | `claim_task`, inside the write txn, after the card is confirmed `ready` | `task_id, board, assignee, workspace_path, claimer` | `None` or `{"refuse": "<reason>"}` |
| `kanban_pre_complete` | start of `complete_task`, after the row is loaded | `task_id, board, assignee, actor, force, summary, metadata, run_id` | `None` or `{"refuse": "<reason>"}` |

Behaviour:

- A `kanban_pre_claim` refusal makes `claim_task` return `None` and leaves the card
  `ready`. A `claim_refused` event `{"reason", "by": "plugin"}` is logged, but only when
  the latest `claim_refused` of the card has a different reason (a dispatcher retrying
  every tick does not flood the log).
- A `kanban_pre_complete` refusal logs a `completion_refused` event and raises the new
  `kanban_db.CompletionRefused(reason)`; the card is not mutated. The `kanban_complete`
  tool returns the reason as a tool error, `hermes kanban complete` prints
  `refused: <reason>` and exits 1, and the dashboard PATCH answers HTTP 409
  `{"error": "completion_refused", "reason": ...}`.
- `complete_task` gains an optional `actor` argument (tool: the profile, CLI:
  `cli:<os user>`, dashboard: `dashboard`) that is passed to the hook.
- Fail-open: a callback that raises, a malformed return value, or a failure of the
  dispatch itself lets the transition proceed as if no hook were registered. The new
  names are deliberately not in `_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS`. Callbacks run on the
  caller thread without a timeout, and `kanban_pre_claim` runs while the SQLite write
  lock is held, so keep callbacks fast.

Files touched in Hermes: `hermes_cli/plugins.py`, `hermes_cli/kanban_db.py`,
`hermes_cli/kanban.py`, `tools/kanban_tools.py`, `plugins/kanban/dashboard/plugin_api.py`
and the new `tests/hermes_cli/test_kanban_veto_hooks.py`. It does not touch
`agent/managed_worker.py`.

Generated against Hermes `bf0203a794` with:

```
git diff bf0203a794..HEAD -- hermes_cli/plugins.py hermes_cli/kanban_db.py tools/kanban_tools.py \
    hermes_cli/kanban.py plugins/kanban/dashboard/plugin_api.py \
    tests/hermes_cli/test_kanban_veto_hooks.py > patches/0001-kanban-veto-hooks.patch
```

### Apply

From the root of the Hermes checkout:

```
git apply --check /path/to/hermes-kanban-empowered/patches/0001-kanban-veto-hooks.patch
git apply /path/to/hermes-kanban-empowered/patches/0001-kanban-veto-hooks.patch
```

Run the new tests with the Hermes interpreter:

```
python -m pytest tests/hermes_cli/test_kanban_veto_hooks.py -q
```

### Verify

```
hermes kanban-empowered doctor
```

`doctor` reports whether `kanban_pre_claim` and `kanban_pre_complete` are present in
`VALID_HOOKS`.

### Reapply after `hermes update`

`hermes update` replaces the checkout, so the patch disappears. Re-run `git apply`
after every update. If `git apply --check` fails, the touched files drifted upstream:
rebase the patch by hand (the change is small and local to the five files above) and
regenerate it with the command above.
