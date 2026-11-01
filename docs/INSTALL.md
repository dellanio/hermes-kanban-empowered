# Install, verify and roll back

Plugins, their state and their settings are per Hermes profile, so every step marked "per profile"
is repeated for each profile that runs kanban workers and for the coordinator profile (default
`default`).

## Requirements

- Hermes Agent at revision `bf0203a794` (`plugin.yaml`: `requires_hermes: ">=0.22"`), kanban enabled.
- `shared_state_dir` on a local NTFS (or other local) disk. The leader lock uses hard links and
  atomic renames; network shares and sync folders do not provide them.
- Python standard library plus modules already shipped with Hermes (`httpx`, `hermes_yaml`); nothing to install into the Hermes venv.

## 1. Apply the core patch (optional, inert in v0.1)

The patch adds the `kanban_pre_claim` / `kanban_pre_complete` veto hooks. v0.1 has no consumer for
them, so skipping it changes nothing except `doctor` showing `veto_hooks: missing`.

```
git -C <hermes-agent> apply --check <repo>/patches/0001-kanban-veto-hooks.patch
git -C <hermes-agent> apply <repo>/patches/0001-kanban-veto-hooks.patch
```

`git apply --check` fails when the patch is already applied. To tell "already applied" from "drifted",
run `git -C <hermes-agent> apply -R --check <repo>/patches/0001-kanban-veto-hooks.patch`: success means
it is already in place. `hermes update` replaces the checkout and drops the patch: re-run the apply
commands after every update (see [`patches/README.md`](../patches/README.md) if `--check` fails on a
checkout without the patch).

## 2. Install the plugin, per profile, pinned to a commit

Pin to a full 40-character commit sha, never to a branch:

```
hermes plugins install <git-url> --ref <40-char sha> --enable --yes-deps
hermes -p <profile> plugins install <git-url> --ref <40-char sha> --enable --yes-deps
```

Run the second form for every executor profile (builder, devops, reviewer, ...). The first form
installs into the active profile (`default`).

## 3. Minimal configuration

Either fill the plugin's settings screen (Hermes Desktop, Capabilities, plugin gear) or edit the
profile's `config.yaml`:

```yaml
plugins:
  entries:
    kanban-empowered:
      settings:
        telegram_owner_id: 123456789   # your Telegram user id (also your private chat id with the bot)
        default_owner: sponsor
        language: en                   # optional; empty follows Hermes
```

Equivalent nested form: `telegram.owner_ids: { sponsor: 123456789 }` and `waits.default_owner`.
The coordinator process also needs `TELEGRAM_BOT_TOKEN` in its environment (the gateway's bot).
Every profile must agree on `coordinator_profile` and `shared_state_dir`; leaving both at their
defaults everywhere is consistent. Full key list: [README](../README.md#configuration).

## 4. Restart the gateway

Restart only when no kanban worker is running, so no run is interrupted:

```
hermes gateway restart
```

## 5. Verify

```
hermes kanban-empowered doctor
```

Expected rows:

| Row | Expected |
|---|---|
| `hermes_compat` / `hermes_head` | `bf0203a794` / the installed revision |
| `veto_hooks` | `present` if the patch is applied, else `missing` (harmless in v0.1) |
| `coordinator` | a PID once the coordinator gateway is running, else `none` |
| `active_profile` / `coordinator_profile` | the profile you ran it in / the configured coordinator |
| `shared_state_dir` | a local path |
| `profiles_with_plugin` | every profile you installed into |
| `telegram_token` | `present` in the coordinator's environment |
| `owner_chats` | `1 configured` or more |
| `telegram_path` | appears after the first Telegram send |

A `config:` row means the settings are invalid; `config_conflict:` rows list form values that
shadow `config.yaml`.

## Uninstall / rollback

1. Per profile: `hermes plugins remove kanban-empowered` (or `hermes -p <profile> plugins remove
   kanban-empowered`).
2. If the core patch was applied, from the Hermes checkout:
   `git -C <hermes-agent> apply -R <repo>/patches/0001-kanban-veto-hooks.patch`.
3. Restore the `config.yaml` backup of each profile (take one before step 3 of the install), or
   delete the `plugins.entries.kanban-empowered` block and the `kanban-empowered` line in
   `plugins.enabled`.
4. `hermes gateway restart` when no worker is running.

The plugin's state (`shared_state_dir`) is separate from the Hermes kanban boards; leaving it in
place is harmless and keeps wait history if you reinstall.
