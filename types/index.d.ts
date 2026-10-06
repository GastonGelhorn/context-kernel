// What the Context Kernel mod keeps in the session's state: the last turn's memory activity as
// `context-kernel activity` reports it, and whether the person folded the band away.
export type Activity = {
  turn: string | null
  line: string
  undo: string[]
  pending: number
  held: number
}

declare module 'claude-code' {
  interface PluginState {
    'context-kernel': { activity: Activity | null; isHidden: boolean }
  }
}
