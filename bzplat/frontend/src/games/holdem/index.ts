/** 德州扑克前端视图规格（封装现有 components/poker/）。 */
import { Spade } from 'lucide-react'
import type { GameViewSpec } from '../base'
import PokerTable from '@/components/poker/PokerTable'
import { reduceEvents } from '@/components/poker/useMatchState'

export const holdemSpec: GameViewSpec = {
  id: 'holdem',
  label: '德州扑克',
  icon: Spade,
  kind: 'cards',
  Board: PokerTable as unknown as GameViewSpec['Board'],
  reduce: reduceEvents as unknown as GameViewSpec['reduce'],
  defaultMatchConfig: { hands: 70 },
  configFields: [
    { key: 'hands', label: '手数', default: 70, min: 1, max: 500 },
  ],
}
