/** 黑白棋前端视图规格（第 4 游戏，对标后端 games/reversi/spec.py）。 */
import { Disc } from 'lucide-react'
import type { GameViewSpec } from '../base'
import { reduceReversiEvents } from './reducer'
import { ReversiCanvasRenderer } from './canvas'

const ReversiBoardStub = () => null  // canvas 接管，DOM Board 不再用

export const reversiSpec: GameViewSpec = {
  id: 'reversi',
  label: '黑白棋',
  icon: Disc,
  kind: 'board',
  Board: ReversiBoardStub as unknown as GameViewSpec['Board'],
  reduce: reduceReversiEvents as unknown as GameViewSpec['reduce'],
  CanvasRenderer: ReversiCanvasRenderer,
  defaultMatchConfig: {},
  configFields: [], // 单局无可调参数
}
