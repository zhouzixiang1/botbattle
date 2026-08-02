/** 点格棋前端视图规格（canvas 渲染；DOM PencilBoard 已删）。 */
import { Circle } from 'lucide-react'
import type { GameViewSpec } from '../base'
import { reducePencilEvents } from '@/components/pencil/usePencilState'
import { PencilCanvasRenderer } from './canvas'

const PencilBoardStub = () => null  // canvas 接管，DOM Board 不再用

export const pencilSpec: GameViewSpec = {
  id: 'pencil',
  label: '点格棋',
  icon: Circle,
  kind: 'board',
  Board: PencilBoardStub as unknown as GameViewSpec['Board'],
  reduce: reducePencilEvents as unknown as GameViewSpec['reduce'],
  CanvasRenderer: PencilCanvasRenderer,
  defaultMatchConfig: { n_dots: 6 }, // 对齐 Botzone grid_size=11 交错 → 6 点 → 25 格
  configFields: [
    { key: 'n_dots', label: '点阵边长', default: 6, min: 3, max: 15 },
  ],
}
