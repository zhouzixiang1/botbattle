/** 德州扑克前端视图规格（封装现有 components/poker/）。 */
import { Spade } from 'lucide-react'
import type { GameViewSpec } from '../base'
import { reduceEvents } from '@/components/poker/useMatchState'
import { PokerCanvasRenderer } from './canvas'

// canvas 接管后 DOM Board 不再用于 holdem；GameViewSpec 要求 Board 必填，给一个空 stub。
const HoldemBoardStub = () => null

export const holdemSpec: GameViewSpec = {
  id: 'holdem',
  label: '德州扑克',
  icon: Spade,
  kind: 'cards',
  Board: HoldemBoardStub,
  reduce: reduceEvents as unknown as GameViewSpec['reduce'],
  CanvasRenderer: PokerCanvasRenderer,
  defaultMatchConfig: { hands: 70 },
  configFields: [
    { key: 'hands', label: '手数', default: 70, min: 1, max: 500 },
  ],
}
