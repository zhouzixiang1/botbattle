/** 德州扑克前端视图规格（reducer 自包含于本子包，对标后端 engine.py）。 */
import { Spade } from 'lucide-react'
import type { GameViewSpec } from '../base'
import { reduceHoldemEvents, type HoldemViewModel } from './reducer'
import { PokerCanvasRenderer } from './canvas'
import { HoldemHumanActions, holdemEndSummary } from './human-actions'
import { describeHoldemEvent, holdemHandBoundaries, HoldemReplaySummary } from './view'

// canvas 接管后 DOM Board 不再用于 holdem；GameViewSpec 要求 Board 必填，给一个空 stub。
const HoldemBoardStub = () => null

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
  humanPlay: {
    layout: 'canvas-controls-log',
    turnLabel: '轮到你操作',
    revealMode: 'showdown',
    ActionPanel: HoldemHumanActions,
    endSummary: holdemEndSummary,
  },
  replay: {
    layout: 'with-timeline',
    progress: (vm) => (vm as HoldemViewModel).hand + 1,
    progressTotal: (vm) => (vm as HoldemViewModel).totalHands,
    Summary: HoldemReplaySummary,
    navigation: {
      unitLabel: '手',
      boundaries: holdemHandBoundaries,
    },
  },
}
