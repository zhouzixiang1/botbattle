/** 五子棋前端视图规格（canvas 渲染；DOM GomokuBoard 已删）。 */
import { Grid3x3 } from 'lucide-react'
import type { GameViewSpec } from '../base'
import { reduceGomokuEvents } from './reducer'
import { GomokuCanvasRenderer } from './canvas'

const GomokuBoardStub = () => null  // canvas 接管，DOM Board 不再用

export const gomokuSpec: GameViewSpec = {
  id: 'gomoku',
  label: '五子棋',
  icon: Grid3x3,
  kind: 'board',
  Board: GomokuBoardStub as unknown as GameViewSpec['Board'],
  reduce: reduceGomokuEvents as unknown as GameViewSpec['reduce'],
  CanvasRenderer: GomokuCanvasRenderer,
  seatColors: ['黑', '白'],
  progressUnit: 'move',
}
