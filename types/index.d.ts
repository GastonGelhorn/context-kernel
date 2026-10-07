// What the Shelflife mod keeps in the session's state: the last turn's memory activity as
// `shelflife-context activity` reports it, and whether the person folded the band away.
export type Activity = {
  turn: string | null
  line: string
  undo: string[]
  pending: number
  held: number
}

declare module 'claude-code' {
  interface PluginState {
    'shelflife-context': { activity: Activity | null; isHidden: boolean }
  }
}
