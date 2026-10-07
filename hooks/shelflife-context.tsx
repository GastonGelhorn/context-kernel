// The Shelflife mod: what memory did, where the person can see it.
//
// The command hooks (hooks.json) do the work: they deliver context, close the turn and write one
// line about what was saved, held or not saved. Claude Code shows that line in the terminal, but the
// desktop app does not, so a fact the person stated could go nowhere without a word. This module
// reads the same line after each turn (`shelflife-context activity`) and draws it above the prompt, with
// a button that takes back what that turn saved. Undo runs the owner's command: a click is the
// person's own act, never the model's.
//
// Needs Claude Code 2.1.287 or later; older versions ignore `modules` and keep the hooks alone.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Activity } from '../types'

type Api = EngineInterface

const activity = atom({ plugin: 'shelflife-context', key: 'activity' } as const, null)
const isHidden = atom({ plugin: 'shelflife-context', key: 'isHidden' } as const, false)

const ACCENT = 'suggestion'

let bandMode = 'on'
let sessionId = ''
let optionEnv: Record<string, string> = {}

async function kernel($: Api, args: string[], timeoutMs = 15_000) {
  const env: Record<string, string> = { ...optionEnv }
  const path = await $.env.get('PATH')
  if (path) env.PATH = path
  try {
    return await $.process.run(['python3', `${$.plugin.root}/bin/shelflife-context`, ...args], { env, timeoutMs })
  } catch {
    return null
  }
}

async function refresh($: Api) {
  if (!sessionId) sessionId = await $.session.id()
  const result = await kernel($, ['activity', '--session', sessionId])
  if (!result || result.exitCode !== 0) return
  let next: Activity
  try {
    next = JSON.parse(result.stdout) as Activity
  } catch {
    return
  }
  const before = await read($, activity)
  await update($, activity, () => next)
  // A new line about a new turn shows again even if the person hid the previous one.
  if (next.line && next.turn !== before?.turn) await update($, isHidden, () => false)
}

async function undo($: Api, ids: string[]) {
  let undone = 0
  for (const id of [...ids].reverse()) {
    const result = await kernel($, ['undo', id])
    if (result && result.exitCode === 0) undone += 1
  }
  $.ui.toast(undone ? `Memory: took back ${undone} fact${undone === 1 ? '' : 's'}.` : 'Memory: nothing to take back.')
  await update($, activity, a => (a ? { ...a, line: undone ? 'Memory: taken back.' : a.line, undo: [] } : a))
}

export const register: Register = (on, options) => {
  bandMode = String(options.band ?? 'on')
  optionEnv = {}
  for (const [key, value] of Object.entries(options)) optionEnv[`CLAUDE_PLUGIN_OPTION_${key.toUpperCase()}`] = String(value)
  sessionId = ''

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    sessionId = await $.session.id()
    return started
  })

  // The Stop command hook writes the turn's line; once it has run, read it.
  on('classic.Stop', async ($, e, next) => {
    const result = await next(e)
    if (bandMode !== 'off') await refresh($)
    return result
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (bandMode === 'off' || e.props.hasSurvey) return next(e)
    const theirs = await next(e) // what other mods draw here stays, under ours
    const now = await read($, activity)
    const quiet = !now || (!now.line && !now.held && !now.pending) || (await read($, isHidden))
    if (quiet) return theirs
    const { Box, Button, Text } = $.ui.resolve(e)
    const waiting = [
      now.held ? `${now.held} held for review` : '',
      now.pending ? `${now.pending} waiting for a yes` : '',
    ].filter(Boolean)
    const text = (now.line || 'Memory:').replace(/ Say "undo" to take it back\.$/, '')
    return (
      <Box flexDirection="column">
        <Box flexDirection="row">
          <Text color={ACCENT} bold>
            ◇{' '}
          </Text>
          <Text wrap="truncate-end">{text}</Text>
          {waiting.length ? <Text dimColor> · {waiting.join(' · ')}</Text> : null}
          <Text> </Text>
          {now.undo.length ? <Button key="undo" label="undo" plain onPress={() => void undo($, now.undo)} /> : null}
          {now.undo.length ? <Text dimColor> · </Text> : null}
          <Button key="hide" label="hide" plain dimColor onPress={() => update($, isHidden, () => true)} />
        </Box>
        {theirs}
      </Box>
    )
  })
}
