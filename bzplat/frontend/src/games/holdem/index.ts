/** 德州扑克前端视图规格（reducer 自包含于本子包，对标后端 engine.py）。 */
import { Spade } from 'lucide-react'
import type { GameViewSpec } from '../base'
import { reduceHoldemEvents, type HoldemViewModel } from './reducer'
import { PokerCanvasRenderer } from './canvas'
import { HoldemHumanActions, holdemEndSummary } from './human-actions'
import { HoldemReplayHud } from './replay-hud'
import { describeHoldemEvent, holdemHandBoundaries, holdemHandLabel } from './view'
import { createTerminalReasonResolver } from '@/games/reasons'

// canvas 接管后 DOM Board 不再用于 holdem；GameViewSpec 要求 Board 必填，给一个空 stub。
const HoldemBoardStub = () => null
const holdemTerminalReason = createTerminalReasonResolver({})

export const holdemSpec: GameViewSpec = {
  id: 'holdem',
  label: '德州扑克',
  icon: Spade,
  kind: 'cards',
  Board: HoldemBoardStub,
  reduce: reduceHoldemEvents as unknown as GameViewSpec['reduce'],
  CanvasRenderer: PokerCanvasRenderer,
  canvasAspectRatio: 16 / 9,
  progressUnit: 'hand',
  matchFormatLabel: '70 手',
  winner: (vm) => (vm as HoldemViewModel).matchWinner,
  describeEvent: describeHoldemEvent,
  terminalReason: holdemTerminalReason,
  humanPlay: {
    layout: 'canvas-controls-log',
    turnLabel: '轮到你操作',
    revealMode: 'showdown',
    ActionPanel: HoldemHumanActions,
    endSummary: holdemEndSummary,
  },
  replay: {
    layout: 'with-timeline',
    progress: (vm) => {
      const state = vm as HoldemViewModel
      return state.handsStarted > 0
        ? state.handsStarted
        : null
    },
    progressTotal: (vm) => {
      const state = vm as HoldemViewModel
      return state.totalHands * state.totalLegs
    },
    Hud: HoldemReplayHud,
    navigation: {
      unitLabel: '手',
      boundaries: holdemHandBoundaries,
      label: holdemHandLabel,
    },
  },
}
