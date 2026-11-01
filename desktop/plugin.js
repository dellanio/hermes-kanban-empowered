/**
 * kanban-empowered — the Desktop half of the plugin: the "Empowered" dashboard.
 *
 * Plain ESM loaded uncompiled by Hermes Desktop (no JSX: jsx()/jsxs() calls). It imports only
 * '@hermes/plugin-sdk', 'react' and 'react/jsx-runtime', and talks only to the plugin's own backend
 * (dashboard/plugin_api.py, mounted at /api/plugins/kanban-empowered) through ctx.rest:
 *
 *   GET  /settings  the two Desktop switches (sidebar entry, Kanban header button) and the language
 *   GET  /i18n      every ui.* string in the plugin language (the brand name is never translated)
 *   GET  /overview  metrics, "needs you", running cards, coordinator timeline, infrastructure (polled)
 *   GET  /count     the number of waits that need you (the Kanban header button's badge)
 *   GET  /item      one item: the reminder text exactly as Telegram shows it, conditions, comments
 *   POST /answer    the owner's answer, classified and applied exactly like a Telegram reply
 *
 * Contributions: a full page at /kanban-empowered, a palette command, a sidebar row (only while the
 * show_sidebar_menu setting is on) and a compact "Empowered" button in the Kanban page header (only
 * while the hash route is /kanban, show_kanban_button is on and this Desktop exports
 * WORKSPACE_PAGE_HEADER_AREA; feature-detected through the namespace import). The button is registered
 * with `when: <route is /kanban>`; because Desktop's router navigates with pushState (no hashchange),
 * a route watcher (hashchange + popstate + a 500 ms ctx.setInterval string compare) re-registers it
 * when the route flips, so the area is rebuilt and the button never lingers on another page. Without
 * the bundled Kanban Desktop plugin the button is inert and the sidebar row still works.
 *
 * Styling uses the app's theme variables only (no hardcoded colours): amber (--ui-yellow) = needs you,
 * red (--ui-red) = incident, green (--ui-green) = running, violet (--ui-purple) = coordinator.
 */

import * as sdk from '@hermes/plugin-sdk'
import {
  Button,
  Codicon,
  ErrorState,
  host,
  PALETTE_AREA,
  ROUTES_AREA,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  SIDEBAR_NAV_AREA,
  Textarea,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useState } from 'react'
import { Fragment, jsx, jsxs } from 'react/jsx-runtime'

const ID = 'kanban-empowered'
const BRAND = 'Kanban Empowered' // product name: never translated
const NAV_LABEL = 'Kanban-Empowered'
const BUTTON_LABEL = 'Empowered'
const ROUTE = '/kanban-empowered'
const KANBAN_ROUTE = '/kanban'
const SETTINGS_ROUTE = '/settings?tab=plugins&agent=' + ID
const POLL_MS = 10000
const SETTINGS_POLL_MS = 30000
const CLOCK_MS = 5000
const ROUTE_POLL_MS = 500

// English text used until GET /i18n answers, and when the backend cannot be reached at all.
const FALLBACK = {
  'ui.subtitle': 'Flow coordinator',
  'ui.filter.all': 'All boards',
  'ui.live': 'Live',
  'ui.offline': 'Offline',
  'ui.loading': 'Loading…',
  'ui.settings': 'Plugin settings',
  'ui.error.title': 'Kanban Empowered is not answering',
  'ui.error.backend': 'Enable the kanban-empowered plugin (plugins.enabled in config.yaml) and restart the gateway.',
  'ui.error.retry': 'Retry',
  'ui.age.s': '{n} s',
  'ui.age.min': '{n} min',
  'ui.age.h': '{n} h',
  'ui.age.d': '{n} d'
}

// Module state: bound in register(), reset on dispose (ES modules are never unloaded).
let pluginCtx = null

function api(path, opts) {
  if (!pluginCtx) {
    return Promise.reject(new Error('kanban-empowered is not loaded'))
  }

  return pluginCtx.rest(path, opts)
}

function storageGet(key, fallback) {
  try {
    return pluginCtx ? pluginCtx.storage.get(key, fallback) : fallback
  } catch (e) {
    return fallback
  }
}

function storageSet(key, value) {
  try {
    if (pluginCtx) {
      pluginCtx.storage.set(key, value)
    }
  } catch (e) {
    // a full or blocked storage only loses the remembered filter
  }
}

// -- text ---------------------------------------------------------------------------------------
function fmt(text, vars) {
  return String(text).replace(/\{(\w+)\}/g, (whole, name) =>
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : whole
  )
}

function makeT(strings) {
  return (key, vars) => fmt(strings[key] ?? FALLBACK[key] ?? key, vars)
}

function ageText(t, seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0))

  if (s < 60) {
    return t('ui.age.s', { n: s })
  }

  if (s < 3600) {
    return t('ui.age.min', { n: Math.floor(s / 60) })
  }

  if (s < 86400) {
    return t('ui.age.h', { n: Math.floor(s / 3600) })
  }

  return t('ui.age.d', { n: Math.floor(s / 86400) })
}

function plural(t, key, n) {
  return t(n === 1 ? key + '.one' : key + '.many', { n })
}

function backendMissing(error) {
  return /\b404\b|not found/i.test(String((error && error.message) || error || ''))
}

// -- data ---------------------------------------------------------------------------------------
function useStrings() {
  const query = useQuery({
    queryKey: [ID, 'i18n'],
    queryFn: () => api('/i18n'),
    staleTime: 300000,
    retry: 1
  })

  return makeT((query.data && query.data.strings) || {})
}

function overviewKey(board) {
  return [ID, 'overview', board || 'all']
}

function useOverview(board, interval) {
  return useQuery({
    queryKey: overviewKey(board),
    queryFn: () => api(board && board !== 'all' ? '/overview?board=' + encodeURIComponent(board) : '/overview'),
    refetchInterval: interval,
    staleTime: Math.floor(interval / 2), // two open windows share one fetch
    retry: 1
  })
}

function useClock() {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!pluginCtx) {
      return undefined
    }

    return pluginCtx.setInterval(() => setNow(Date.now()), CLOCK_MS)
  }, [])

  return now
}

// -- styles (scoped to .ke-root; theme variables only) -------------------------------------------
const CSS = `
.ke-root { container-type: inline-size; position: relative; height: 100%; overflow: hidden;
  color: var(--ui-text-primary); font-size: 0.8125rem; }
.ke-scroll { height: 100%; overflow: auto; }
.ke-wrap { display: flex; flex-direction: column; gap: 14px; padding: 16px 20px 28px; min-width: 0; }
.ke-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.ke-brand { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.ke-brand-name { font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; font-size: 0.875rem; }
.ke-brand-sub { color: var(--ui-text-tertiary); }
.ke-tools { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ke-live { display: inline-flex; align-items: center; gap: 6px; color: var(--ui-text-secondary); font-size: 0.75rem; }
.ke-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; flex: none; }
.ke-dot-green { background: var(--ui-green); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ui-green) 22%, transparent); }
.ke-dot-red { background: var(--ui-red); }
.ke-dot-violet { background: var(--ui-purple); }
.ke-dot-amber { background: var(--ui-yellow); }
.ke-muted { color: var(--ui-text-tertiary); }
.ke-faint { color: var(--ui-text-quaternary); }
.ke-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.ke-metric { border: 1px solid var(--ui-stroke-tertiary); border-radius: 8px; padding: 10px 12px;
  background: var(--ui-bg-quaternary); border-top: 2px solid var(--ke-tone, var(--ui-stroke-secondary)); }
.ke-metric-value { font-size: 1.5rem; font-weight: 650; line-height: 1.2; color: var(--ke-tone, var(--ui-text-primary)); }
.ke-metric-label { color: var(--ui-text-secondary); font-size: 0.75rem; }
.ke-cols { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr); gap: 14px; align-items: start; }
.ke-col { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.ke-section { border: 1px solid var(--ui-stroke-tertiary); border-radius: 10px; background: var(--ui-bg-quinary); min-width: 0; }
.ke-section-head { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-bottom: 1px solid var(--ui-stroke-tertiary); }
.ke-section-title { font-weight: 600; }
.ke-count { min-width: 20px; padding: 0 6px; border-radius: 999px; font-size: 0.6875rem; line-height: 18px; text-align: center;
  background: color-mix(in srgb, var(--ke-tone, var(--ui-text-tertiary)) 18%, transparent); color: var(--ke-tone, var(--ui-text-secondary)); }
.ke-section-body { padding: 10px 12px; display: flex; flex-direction: column; gap: 10px; }
.ke-empty { color: var(--ui-text-tertiary); padding: 6px 0; }
.ke-card { border: 1px solid color-mix(in srgb, var(--ke-tone) 35%, var(--ui-stroke-tertiary)); border-left: 3px solid var(--ke-tone);
  border-radius: 8px; padding: 10px 12px; background: color-mix(in srgb, var(--ke-tone) 6%, transparent);
  display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.ke-card-top { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }
.ke-card-title { font-weight: 600; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ke-chips { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.ke-chip { font-size: 0.6875rem; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--ui-stroke-tertiary);
  color: var(--ui-text-secondary); white-space: nowrap; max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
.ke-chip-tone { border-color: color-mix(in srgb, var(--ke-tone) 45%, transparent); color: var(--ke-tone);
  background: color-mix(in srgb, var(--ke-tone) 12%, transparent); }
.ke-need { color: var(--ui-text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ke-options { display: flex; gap: 6px; flex-wrap: wrap; }
.ke-card-foot { display: flex; justify-content: space-between; align-items: center; gap: 8px; color: var(--ui-text-tertiary); font-size: 0.75rem; }
.ke-table { display: flex; flex-direction: column; }
.ke-tr { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 0.8fr) minmax(0, 1.4fr); gap: 10px; padding: 7px 0;
  border-bottom: 1px solid var(--ui-stroke-tertiary); align-items: start; }
.ke-tr:last-child { border-bottom: none; }
.ke-th { color: var(--ui-text-tertiary); font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.04em; padding-top: 0; }
.ke-cell { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.ke-cell-main { display: flex; gap: 6px; align-items: baseline; min-width: 0; }
.ke-ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ke-sub { color: var(--ui-text-tertiary); font-size: 0.75rem; }
.ke-tl { display: flex; flex-direction: column; gap: 0; }
.ke-tl-row { display: grid; grid-template-columns: 12px minmax(0, 1fr) auto; gap: 8px; padding: 6px 0; align-items: baseline; }
.ke-kv { display: flex; justify-content: space-between; gap: 10px; padding: 3px 0; }
.ke-tone-red { color: var(--ui-red); }
.ke-tone-green { color: var(--ui-green); }
.ke-tone-amber { color: var(--ui-yellow); }
.ke-backdrop { position: absolute; inset: 0; background: color-mix(in srgb, var(--ui-bg-elevated) 55%, transparent); z-index: 20; }
.ke-drawer { position: absolute; top: 0; right: 0; bottom: 0; width: min(460px, 100%); z-index: 21; overflow: auto;
  background: var(--ui-bg-elevated); border-left: 1px solid var(--ui-stroke-secondary); display: flex; flex-direction: column; }
.ke-drawer-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; padding: 14px 16px;
  border-bottom: 1px solid var(--ui-stroke-tertiary); }
.ke-drawer-body { padding: 14px 16px 20px; display: flex; flex-direction: column; gap: 14px; }
.ke-label { color: var(--ui-text-tertiary); font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.05em; }
.ke-reminder { white-space: pre-wrap; line-height: 1.45; padding: 10px 12px; border-radius: 8px;
  background: color-mix(in srgb, var(--ke-tone) 8%, transparent); border: 1px solid color-mix(in srgb, var(--ke-tone) 30%, transparent); }
.ke-opt-on { box-shadow: inset 0 0 0 1px var(--ui-yellow); color: var(--ui-yellow); }
.ke-result { padding: 8px 10px; border-radius: 6px; white-space: pre-wrap;
  background: color-mix(in srgb, var(--ke-tone) 10%, transparent); color: var(--ke-tone); }
.ke-details summary { cursor: pointer; color: var(--ui-text-secondary); }
.ke-cond { display: grid; grid-template-columns: 16px 90px minmax(0, 1fr); gap: 6px; padding: 3px 0; }
.ke-comment { border-left: 2px solid var(--ui-stroke-secondary); padding: 2px 0 2px 8px; }
.ke-error { padding: 40px 16px; }
@container (max-width: 900px) {
  .ke-cols { grid-template-columns: minmax(0, 1fr); }
  .ke-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ke-wrap { padding: 12px 12px 24px; }
}
@container (max-width: 520px) {
  .ke-tr { grid-template-columns: minmax(0, 1fr); gap: 2px; }
  .ke-th { display: none; }
}
`

const TONE = {
  amber: { '--ke-tone': 'var(--ui-yellow)' },
  red: { '--ke-tone': 'var(--ui-red)' },
  green: { '--ke-tone': 'var(--ui-green)' },
  violet: { '--ke-tone': 'var(--ui-purple)' },
  neutral: { '--ke-tone': 'var(--ui-text-tertiary)' }
}

// -- small pieces -------------------------------------------------------------------------------
function Section({ title, count, tone, children, extra }) {
  return jsxs('section', {
    className: 'ke-section',
    style: TONE[tone] || TONE.neutral,
    children: [
      jsxs('div', {
        className: 'ke-section-head',
        children: [
          jsx('span', { className: 'ke-section-title', children: title }),
          count === undefined ? null : jsx('span', { className: 'ke-count', children: String(count) }),
          extra ? jsx('span', { style: { marginLeft: 'auto' }, children: extra }) : null
        ]
      }),
      jsx('div', { className: 'ke-section-body', children })
    ]
  })
}

function Metric({ label, value, tone }) {
  return jsxs('div', {
    className: 'ke-metric',
    style: TONE[tone] || TONE.neutral,
    children: [
      jsx('div', { className: 'ke-metric-value', children: String(value ?? 0) }),
      jsx('div', { className: 'ke-metric-label', children: label })
    ]
  })
}

function Empty({ text }) {
  return jsx('div', { className: 'ke-empty', children: text })
}

// -- needs you ----------------------------------------------------------------------------------
function NeedCard({ item, t, onOpen }) {
  if (item.kind === 'infra_exhausted') {
    return jsxs('div', {
      className: 'ke-card',
      style: TONE.red,
      children: [
        jsxs('div', {
          className: 'ke-card-top',
          children: [
            jsx('span', { className: 'ke-card-title', title: item.title, children: item.title || item.board_title }),
            jsx('span', { className: 'ke-muted', children: t('ui.needs.ago', { age: item.age_text || ageText(t, item.age_s) }) })
          ]
        }),
        jsxs('div', {
          className: 'ke-chips',
          children: [
            jsx('span', { className: 'ke-chip', children: item.board_title }),
            jsx('span', { className: 'ke-chip ke-chip-tone', children: t('ui.needs.exhausted') }),
            item.held_behind_count
              ? jsx('span', { className: 'ke-chip', children: plural(t, 'ui.needs.blocks', item.held_behind_count) })
              : null
          ]
        }),
        jsx('div', { className: 'ke-need', title: item.need, children: item.need }),
        jsxs('div', {
          className: 'ke-card-foot',
          children: [
            jsx('span', { children: item.attempts_text }),
            jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => onOpen(item, null), children: t('ui.needs.details') })
          ]
        })
      ]
    })
  }

  const reminder = item.reminder_sent
    ? t('ui.needs.reminder_sent', { age: item.next_reminder_text || ageText(t, item.next_reminder_in_s) })
    : item.next_reminder_in_s > 0
      ? t('ui.needs.reminder_next', { age: item.next_reminder_text || ageText(t, item.next_reminder_in_s) })
      : t('ui.needs.reminder_overdue')

  return jsxs('div', {
    className: 'ke-card',
    style: TONE.amber,
    children: [
      jsxs('div', {
        className: 'ke-card-top',
        children: [
          jsx('span', { className: 'ke-card-title', title: item.title, children: item.title || item.board_title }),
          jsx('span', { className: 'ke-muted', children: t('ui.needs.ago', { age: item.age_text || ageText(t, item.age_s) }) })
        ]
      }),
      jsxs('div', {
        className: 'ke-chips',
        children: [
          jsx('span', { className: 'ke-chip', title: item.board_title, children: item.board_title }),
          item.held_behind_count
            ? jsx('span', { className: 'ke-chip ke-chip-tone', children: plural(t, 'ui.needs.blocks', item.held_behind_count) })
            : null
        ]
      }),
      jsx('div', { className: 'ke-need', title: item.need, children: item.need }),
      item.options && item.options.length
        ? jsx('div', {
            className: 'ke-options',
            children: item.options.map(opt =>
              jsx(
                Button,
                {
                  size: 'xs',
                  variant: 'chip',
                  title: opt.letter + ' — ' + opt.label,
                  onClick: () => onOpen(item, opt.letter),
                  children: opt.letter + ' — ' + opt.label
                },
                opt.letter
              )
            )
          })
        : null,
      jsxs('div', {
        className: 'ke-card-foot',
        children: [
          jsx('span', { children: reminder }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => onOpen(item, null), children: t('ui.needs.details') })
        ]
      })
    ]
  })
}

function NeedsYou({ items, t, onOpen }) {
  const waits = items.filter(i => i.kind === 'human_wait').length

  return jsx(Section, {
    title: t('ui.section.needs_you'),
    count: waits,
    tone: 'amber',
    children: items.length
      ? items.map(item => jsx(NeedCard, { item, t, onOpen }, item.board + '/' + item.task_id))
      : jsx(Empty, { text: t('ui.needs.empty') })
  })
}

// -- running ------------------------------------------------------------------------------------
function Running({ rows, t }) {
  const head = jsxs('div', {
    className: 'ke-tr ke-th',
    children: [
      jsx('span', { children: t('ui.running.card') }),
      jsx('span', { children: t('ui.running.board') }),
      jsx('span', { children: t('ui.running.activity') })
    ]
  })

  return jsx(Section, {
    title: t('ui.section.running'),
    count: rows.length,
    tone: 'green',
    children: rows.length
      ? jsxs('div', {
          className: 'ke-table',
          children: [
            head,
            ...rows.map(r =>
              jsxs(
                'div',
                {
                  className: 'ke-tr',
                  children: [
                    jsxs('div', {
                      className: 'ke-cell',
                      children: [
                        jsxs('div', {
                          className: 'ke-cell-main',
                          children: [
                            jsx('span', { className: 'ke-dot ke-dot-green' }),
                            jsx('span', { className: 'ke-ellipsis', title: r.title, children: r.title })
                          ]
                        }),
                        jsx('div', { className: 'ke-sub ke-ellipsis', children: r.profile || '—' })
                      ]
                    }),
                    jsx('div', { className: 'ke-cell ke-ellipsis ke-muted', title: r.board_name, children: r.board_name }),
                    jsxs('div', {
                      className: 'ke-cell',
                      children: [
                        jsx('div', { className: 'ke-ellipsis', title: r.last_activity.text, children: r.last_activity.text }),
                        jsx('div', {
                          className: 'ke-sub',
                          children:
                            t('ui.needs.ago', { age: ageText(t, r.last_activity.age_s) }) +
                            (r.last_activity.heartbeat_age_s !== null && r.last_activity.heartbeat_age_s !== undefined
                              ? ' · ' + t('ui.activity.heartbeat', { age: ageText(t, r.last_activity.heartbeat_age_s) })
                              : '')
                        })
                      ]
                    })
                  ]
                },
                r.board + '/' + r.task_id
              )
            )
          ]
        })
      : jsx(Empty, { text: t('ui.running.empty') })
  })
}

// -- coordinator timeline -----------------------------------------------------------------------
function Timeline({ entries, t, nowS }) {
  return jsx(Section, {
    title: t('ui.section.timeline'),
    tone: 'violet',
    children: entries.length
      ? jsx('div', {
          className: 'ke-tl',
          children: entries.map((e, i) =>
            jsxs(
              'div',
              {
                className: 'ke-tl-row',
                children: [
                  jsx('span', { className: 'ke-dot ke-dot-violet' }),
                  jsxs('div', {
                    style: { minWidth: 0 },
                    children: [
                      jsx('div', { children: e.text }),
                      e.title || e.board
                        ? jsx('div', {
                            className: 'ke-sub ke-ellipsis',
                            title: e.title,
                            children: [e.title, e.board].filter(Boolean).join(' · ')
                          })
                        : null
                    ]
                  }),
                  jsx('span', { className: 'ke-sub', children: t('ui.needs.ago', { age: ageText(t, nowS - e.ts) }) })
                ]
              },
              e.kind + ':' + e.ts + ':' + i
            )
          )
        })
      : jsx(Empty, { text: t('ui.timeline.empty') })
  })
}

// -- infrastructure -----------------------------------------------------------------------------
function Infra({ infra, t }) {
  const path = infra.telegram_path
  const pathText = !infra.telegram_token
    ? t('ui.infra.telegram.no_token')
    : path
      ? t('ui.infra.telegram.' + path)
      : t('ui.infra.telegram.none')
  const kv = (label, value, cls) =>
    jsxs('div', {
      className: 'ke-kv',
      children: [jsx('span', { className: 'ke-muted', children: label }), jsx('span', { className: cls || '', children: value })]
    })

  return jsx(Section, {
    title: t('ui.section.infra'),
    tone: infra.systemic_alert_active || infra.budget_exhausted.length ? 'red' : 'neutral',
    children: jsxs(Fragment, {
      children: [
        kv(t('ui.infra.recent'), String(infra.recent_infra_exits_1h), infra.recent_infra_exits_1h ? 'ke-tone-red' : ''),
        kv(
          t('ui.infra.systemic'),
          infra.systemic_alert_active ? t('ui.infra.systemic.on') : t('ui.infra.systemic.off'),
          infra.systemic_alert_active ? 'ke-tone-red' : 'ke-tone-green'
        ),
        kv(t('ui.infra.telegram'), pathText, path === 'ip' ? 'ke-tone-amber' : infra.telegram_ok ? 'ke-tone-green' : ''),
        jsx('div', { className: 'ke-label', style: { marginTop: 4 }, children: t('ui.infra.budget') }),
        infra.budget_exhausted.length
          ? infra.budget_exhausted.map(row =>
              jsxs(
                'div',
                {
                  className: 'ke-cell-main',
                  children: [
                    jsx('span', { className: 'ke-dot ke-dot-red' }),
                    jsx('span', { className: 'ke-ellipsis', title: row.title, children: row.title }),
                    jsx('span', { className: 'ke-sub', children: row.board + ' · ' + plural(t, 'ui.infra.attempts', row.attempts) })
                  ]
                },
                row.board + '/' + row.task_id
              )
            )
          : jsx('div', { className: 'ke-sub', children: t('ui.infra.budget.none') })
      ]
    })
  })
}

// -- detail drawer ------------------------------------------------------------------------------
function Conditions({ list, t }) {
  return jsxs('details', {
    className: 'ke-details',
    children: [
      jsx('summary', { children: t('ui.drawer.conditions') }),
      jsx('div', {
        style: { marginTop: 6 },
        children: list.map(c =>
          jsxs(
            'div',
            {
              className: 'ke-cond',
              children: [
                jsx('span', { className: c.ok ? 'ke-tone-green' : c.warn ? 'ke-tone-amber' : 'ke-tone-red', children: c.ok ? '✓' : c.warn ? '⚠' : '✗' }),
                jsx('span', { className: 'ke-muted', children: c.name }),
                jsx('span', { style: { minWidth: 0, overflowWrap: 'anywhere' }, children: c.text })
              ]
            },
            c.name
          )
        )
      })
    ]
  })
}

function Comments({ list, t }) {
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 8 },
    children: [
      jsx('div', { className: 'ke-label', children: t('ui.drawer.comments') }),
      list.length
        ? list.map((c, i) =>
            jsxs(
              'div',
              {
                className: 'ke-comment',
                children: [
                  jsx('div', { className: 'ke-sub', children: c.author + ' · ' + t('ui.needs.ago', { age: ageText(t, c.age_s) }) }),
                  jsx('div', { style: { overflowWrap: 'anywhere' }, children: c.body })
                ]
              },
              c.ts + ':' + i
            )
          )
        : jsx('div', { className: 'ke-sub', children: t('ui.drawer.no_comments') })
    ]
  })
}

const RESULT_TONE = { resolved: 'green', noted: 'amber', ambiguous: 'amber', changed: 'red', not_found: 'red', error: 'red' }

function Drawer({ target, t, onClose }) {
  const queryClient = useQueryClient()
  const detail = useQuery({
    queryKey: [ID, 'item', target.board, target.task_id],
    queryFn: () =>
      api('/item?board=' + encodeURIComponent(target.board) + '&task_id=' + encodeURIComponent(target.task_id)),
    retry: false
  })
  const [choice, setChoice] = useState(target.option || null)
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [result, setResult] = useState(null)

  useEffect(() => {
    if (!pluginCtx) {
      return undefined
    }

    return pluginCtx.addEventListener(window, 'keydown', event => {
      if (event.key === 'Escape') {
        onClose()
      }
    })
  }, [onClose])

  const item = detail.data
  const human = item && item.kind === 'human_wait'
  const canSend = human && !sending && (choice || text.trim())

  const send = () => {
    if (!canSend) {
      return
    }

    setSending(true)
    setResult(null)
    const body = { board: item.board, task_id: item.task_id, version: item.version }

    if (choice) {
      body.option = choice
    } else {
      body.text = text.trim()
    }

    api('/answer', { method: 'POST', body })
      .then(res => {
        queryClient.invalidateQueries({ queryKey: [ID, 'overview'] })
        queryClient.invalidateQueries({ queryKey: [ID, 'count'] })

        if (res.result === 'resolved') {
          host.notify({ kind: 'success', message: res.message })
          onClose()

          return
        }

        queryClient.invalidateQueries({ queryKey: [ID, 'item', target.board, target.task_id] })
        setResult(res)

        if (res.result === 'noted') {
          setText('')
        }
      })
      .catch(error => setResult({ result: 'error', message: t('ui.answer.failed', { error: String((error && error.message) || error) }) }))
      .finally(() => setSending(false))
  }

  let body

  if (detail.isLoading) {
    body = jsx('div', { className: 'ke-muted', children: t('ui.loading') })
  } else if (detail.error || !item) {
    body = jsx('div', { className: 'ke-result', style: TONE.red, children: t('ui.answer.not_found') })
  } else {
    const tone = human ? 'amber' : 'red'
    const meta = [
      t('ui.needs.ago', { age: item.age_text || ageText(t, item.age_s) }),
      item.held_behind_count ? plural(t, 'ui.needs.blocks', item.held_behind_count) : null,
      human ? t('ui.drawer.stage', { stage: item.stage_text }) : item.attempts_text
    ].filter(Boolean)

    body = jsxs(Fragment, {
      children: [
        jsxs('div', {
          children: [
            jsx('div', { className: 'ke-label', children: t('ui.drawer.why') }),
            jsx('div', { className: 'ke-reminder', style: { ...TONE[tone], marginTop: 6 }, children: item.reminder_text }),
            jsx('div', { className: 'ke-sub', style: { marginTop: 6 }, children: meta.join(' · ') })
          ]
        }),
        human && item.options && item.options.length
          ? jsxs('div', {
              children: [
                jsx('div', { className: 'ke-label', children: t('ui.drawer.options') }),
                jsx('div', {
                  className: 'ke-options',
                  style: { marginTop: 6 },
                  children: item.options.map(opt =>
                    jsx(
                      Button,
                      {
                        size: 'sm',
                        variant: 'chip',
                        className: choice === opt.letter ? 'ke-opt-on' : undefined,
                        'aria-pressed': choice === opt.letter,
                        onClick: () => {
                          setChoice(choice === opt.letter ? null : opt.letter)
                          setText('')
                        },
                        children: opt.letter + ' — ' + opt.label
                      },
                      opt.letter
                    )
                  )
                })
              ]
            })
          : null,
        human
          ? jsxs('div', {
              style: { display: 'flex', flexDirection: 'column', gap: 6 },
              children: [
                jsx('div', { className: 'ke-label', children: t('ui.drawer.reply') }),
                jsx(Textarea, {
                  rows: 4,
                  value: text,
                  maxLength: 4000,
                  placeholder:
                    item.options && item.options.length
                      ? t('ui.drawer.reply_hint_options')
                      : t('ui.drawer.reply_hint', { word: item.reply_hint }),
                  onChange: event => {
                    setText(event.target.value)

                    if (event.target.value) {
                      setChoice(null)
                    }
                  },
                  onKeyDown: event => {
                    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                      send()
                    }
                  }
                }),
                jsxs('div', {
                  style: { display: 'flex', justifyContent: 'flex-end', gap: 8 },
                  children: [
                    jsx(Button, { variant: 'ghost', size: 'sm', onClick: onClose, children: t('ui.drawer.close') }),
                    jsx(Button, {
                      size: 'sm',
                      disabled: !canSend,
                      onClick: send,
                      children: sending ? t('ui.drawer.sending') : t('ui.drawer.send')
                    })
                  ]
                }),
                result
                  ? jsx('div', { className: 'ke-result', style: TONE[RESULT_TONE[result.result] || 'neutral'], children: result.message })
                  : null
              ]
            })
          : null,
        item.conditions ? jsx(Conditions, { list: item.conditions, t }) : null,
        item.comments ? jsx(Comments, { list: item.comments, t }) : null
      ]
    })
  }

  return jsxs(Fragment, {
    children: [
      jsx('div', { className: 'ke-backdrop', onClick: onClose }),
      jsxs('aside', {
        className: 'ke-drawer',
        role: 'dialog',
        'aria-modal': true,
        'aria-label': (item && item.title) || target.task_id,
        children: [
          jsxs('div', {
            className: 'ke-drawer-head',
            children: [
              jsxs('div', {
                style: { minWidth: 0 },
                children: [
                  jsx('div', { className: 'ke-label', children: t('ui.drawer.project') }),
                  jsx('div', { style: { fontWeight: 600 }, children: (item && item.board_name) || target.board }),
                  item && item.title
                    ? jsx('div', { className: 'ke-label', style: { marginTop: 8 }, children: t('ui.drawer.card') })
                    : null,
                  item && item.title
                    ? jsx('div', { className: 'ke-muted', style: { marginTop: 2, overflowWrap: 'anywhere' }, children: item.title })
                    : null
                ]
              }),
              jsx(Button, {
                variant: 'ghost',
                size: 'icon-xs',
                'aria-label': t('ui.drawer.close'),
                title: t('ui.drawer.close'),
                onClick: onClose,
                children: jsx(Codicon, { name: 'close' })
              })
            ]
          }),
          jsx('div', { className: 'ke-drawer-body', children: body })
        ]
      })
    ]
  })
}

// -- the page -----------------------------------------------------------------------------------
function BoardFilter({ boards, names, value, onChange, t }) {
  return jsx(Select, {
    value,
    onValueChange: onChange,
    children: [
      jsx(SelectTrigger, { style: { minWidth: 170 }, 'aria-label': t('ui.filter.all'), children: jsx(SelectValue, {}) }, 'trigger'),
      jsx(
        SelectContent,
        {
          children: [
            jsx(SelectItem, { value: 'all', children: t('ui.filter.all') }, 'all'),
            ...boards.map(b => jsx(SelectItem, { value: b, children: (names && names[b]) || b }, b))
          ]
        },
        'content'
      )
    ]
  })
}

function EmpoweredPage() {
  const t = useStrings()
  const now = useClock()
  const [board, setBoardState] = useState(() => storageGet('board', 'all'))
  const [open, setOpen] = useState(null)
  const overview = useOverview(board, POLL_MS)
  const data = overview.data

  const setBoard = value => {
    setBoardState(value)
    storageSet('board', value)
  }

  useEffect(() => {
    // a remembered board that no longer exists falls back to every board
    if (board !== 'all' && overview.error && backendMissing(overview.error) && data === undefined) {
      setBoard('all')
    }
  }, [overview.error])

  const onOpen = (item, option) => setOpen({ board: item.board, task_id: item.task_id, option })
  const onClose = useCallback(() => setOpen(null), [])
  const nowS = Math.floor(now / 1000)
  const live = !overview.error && data
  const updated = overview.dataUpdatedAt ? Math.max(0, Math.floor((now - overview.dataUpdatedAt) / 1000)) : null

  const header = jsxs('div', {
    className: 'ke-header',
    children: [
      jsxs('div', {
        className: 'ke-brand',
        children: [
          jsx('span', { className: 'ke-brand-name', children: BRAND }),
          jsx('span', { className: 'ke-faint', children: '·' }),
          jsx('span', { className: 'ke-brand-sub', children: t('ui.subtitle') })
        ]
      }),
      jsxs('div', {
        className: 'ke-tools',
        children: [
          data ? jsx(BoardFilter, { boards: data.boards || [], names: data.board_names, value: board, onChange: setBoard, t }) : null,
          jsxs('span', {
            className: 'ke-live',
            children: [
              jsx('span', { className: live ? 'ke-dot ke-dot-green' : 'ke-dot ke-dot-red' }),
              jsx('span', { children: live ? t('ui.live') : t('ui.offline') }),
              live && updated !== null ? jsx('span', { className: 'ke-faint', children: t('ui.updated', { age: ageText(t, updated) }) }) : null
            ]
          }),
          jsx(Button, {
            variant: 'ghost',
            size: 'icon-xs',
            title: t('ui.settings'),
            'aria-label': t('ui.settings'),
            onClick: () => host.navigate(SETTINGS_ROUTE),
            children: jsx(Codicon, { name: 'gear' })
          })
        ]
      })
    ]
  })

  let content

  if (!data && overview.isLoading) {
    content = jsx('div', { className: 'ke-muted', children: t('ui.loading') })
  } else if (!data) {
    content = jsx('div', {
      className: 'ke-error',
      children: jsx(ErrorState, {
        title: t('ui.error.title'),
        description: backendMissing(overview.error)
          ? t('ui.error.backend')
          : String((overview.error && overview.error.message) || overview.error || ''),
        children: jsx('div', {
          style: { display: 'flex', justifyContent: 'center' },
          children: jsx(Button, { size: 'sm', onClick: () => overview.refetch(), children: t('ui.error.retry') })
        })
      })
    })
  } else {
    const m = data.metrics || {}
    content = jsxs(Fragment, {
      children: [
        jsxs('div', {
          className: 'ke-metrics',
          children: [
            jsx(Metric, { label: t('ui.metric.needs_you'), value: m.needs_you, tone: 'amber' }),
            jsx(Metric, { label: t('ui.metric.impacted'), value: m.impacted_cards, tone: 'amber' }),
            jsx(Metric, { label: t('ui.metric.workers'), value: m.workers_running, tone: 'green' }),
            jsx(Metric, { label: t('ui.metric.incidents'), value: m.incidents, tone: m.incidents ? 'red' : 'neutral' })
          ]
        }),
        jsxs('div', {
          className: 'ke-cols',
          children: [
            jsxs('div', {
              className: 'ke-col',
              children: [
                jsx(NeedsYou, { items: data.needs_you || [], t, onOpen }),
                jsx(Running, { rows: data.running || [], t })
              ]
            }),
            jsxs('div', {
              className: 'ke-col',
              children: [
                jsx(Timeline, { entries: data.timeline || [], t, nowS }),
                jsx(Infra, { infra: data.infra, t })
              ]
            })
          ]
        })
      ]
    })
  }

  return jsxs('div', {
    className: 'ke-root',
    children: [
      jsx('style', { children: CSS }),
      jsx('div', { className: 'ke-scroll', children: jsxs('div', { className: 'ke-wrap', children: [header, content] }) }),
      open ? jsx(Drawer, { target: open, t, onClose }, open.board + '/' + open.task_id + '/' + (open.option || '')) : null
    ]
  })
}

// -- the button in the Kanban page header --------------------------------------------------------
// Rendered in the Kanban page's header, outside .ke-root: inline styles only.
const COUNT_CHIP = {
  minWidth: 16,
  padding: '0 5px',
  borderRadius: 999,
  fontSize: '0.625rem',
  lineHeight: '15px',
  textAlign: 'center',
  color: 'var(--ui-yellow)',
  background: 'color-mix(in srgb, var(--ui-yellow) 18%, transparent)'
}

function KanbanButton() {
  // the light GET /count, not the whole overview (fleet scan, infra assessment, comments)
  const badge = useQuery({ queryKey: [ID, 'count'], queryFn: () => api('/count'), refetchInterval: 30000, retry: 1 })
  const count = badge.data ? badge.data.needs_you : 0

  return jsx(Button, {
    variant: 'ghost',
    size: 'xs',
    title: BRAND,
    onClick: () => host.navigate(ROUTE),
    children: [
      jsx(Codicon, { name: 'checklist', size: '0.8rem' }, 'icon'),
      jsx('span', { children: BUTTON_LABEL }, 'label'),
      count
        ? jsx('span', { style: COUNT_CHIP, children: String(count) }, 'count')
        : null
    ]
  })
}

function currentRoute() {
  const hash = (window.location && window.location.hash) || ''
  const path = hash.replace(/^#/, '')
  const cut = path.search(/[?#]/)

  return cut === -1 ? path : path.slice(0, cut)
}

export default {
  id: 'kanban-empowered',
  name: BRAND,
  description: 'The Empowered dashboard: what needs you, running cards, coordinator actions and infrastructure, and answers typed in the app.',
  register(ctx) {
    pluginCtx = ctx

    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: ROUTE },
        render: () => jsx(EmpoweredPage, {})
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'kanban-empowered.open',
          label: BRAND,
          keywords: ['kanban', 'empowered', 'needs you', 'precisa de você', 'dashboard'],
          run: () => host.navigate(ROUTE)
        }
      }
    ])

    // The Kanban header area is new in this SDK release: feature-detected, never a named import.
    const headerArea = sdk.WORKSPACE_PAGE_HEADER_AREA ?? null
    const prefs = { nav: true, button: true }
    let disposeNav = null
    let disposeButton = null

    const syncNav = () => {
      if (prefs.nav && !disposeNav) {
        disposeNav = ctx.register({
          id: 'nav',
          area: SIDEBAR_NAV_AREA,
          order: 55,
          data: { path: ROUTE, label: NAV_LABEL, codicon: 'checklist' }
        })
      } else if (!prefs.nav && disposeNav) {
        disposeNav()
        disposeNav = null
      }
    }

    // Desktop's HashRouter navigates with history.pushState, which fires neither hashchange nor popstate.
    // So the button is registered once, with `when` (the registry re-evaluates it whenever the header area
    // is rebuilt; the Kanban page's own board switcher mounts and unmounts there), and a route watcher
    // re-registers it when the route flips without such a rebuild: hashchange (host.navigate), popstate
    // (back/forward) and a cheap 500 ms poll (sidebar, palette, keybinds).
    const onKanban = () => currentRoute() === KANBAN_ROUTE
    let lastOnKanban = onKanban()
    const registerButton = () =>
      ctx.register({
        id: 'kanban-button',
        area: headerArea,
        order: 10,
        when: onKanban,
        render: () => (onKanban() ? jsx(KanbanButton, {}) : null)
      })

    const syncButton = () => {
      const wanted = Boolean(headerArea) && prefs.button

      if (wanted && !disposeButton) {
        lastOnKanban = onKanban()
        disposeButton = registerButton()
      } else if (!wanted && disposeButton) {
        disposeButton()
        disposeButton = null
      }
    }

    const checkRoute = () => {
      const now = onKanban()

      if (now === lastOnKanban) {
        return
      }

      lastOnKanban = now

      if (disposeButton) {
        disposeButton()
        disposeButton = registerButton()
      }
    }

    const loadSettings = () =>
      ctx
        .rest('/settings')
        .then(s => {
          prefs.nav = !(s && s.show_sidebar_menu === false)
          prefs.button = !(s && s.show_kanban_button === false)
          syncNav()
          syncButton()
        })
        .catch(() => {
          // backend off: keep the defaults (the page shows the error state)
        })

    syncNav()
    syncButton()
    ctx.addEventListener(window, 'hashchange', checkRoute)
    ctx.addEventListener(window, 'popstate', checkRoute)
    ctx.setInterval(checkRoute, ROUTE_POLL_MS)
    loadSettings()
    ctx.setInterval(loadSettings, SETTINGS_POLL_MS)

    if (typeof ctx.onEvent === 'function') {
      ctx.onEvent('plugin.kanban-empowered.settings.changed', () => loadSettings())
    }

    ctx.onDispose(() => {
      if (disposeNav) {
        disposeNav()
      }

      if (disposeButton) {
        disposeButton()
      }

      disposeNav = null
      disposeButton = null
      pluginCtx = null
    })
  }
}
