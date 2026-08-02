/** 五子棋前端视图规格（封装现有 components/gomoku/）。 */
import { Grid3x3 } from 'lucide-react'
import type { GameViewSpec } from '../base'
import GomokuBoard from '@/components/gomoku/GomokuBoard'
import { reduceGomokuEvents } from '@/components/gomoku/useGomokuState'

export const gomokuSpec: GameViewSpec = {
  id: 'gomoku',
  label: '五子棋',
  icon: Grid3x3,
  kind: 'board',
  Board: GomokuBoard as unknown as GameViewSpec['Board'],
  reduce: reduceGomokuEvents as unknown as GameViewSpec['reduce'],
  defaultMatchConfig: {},
  configFields: [], // 单局无可调参数
}
