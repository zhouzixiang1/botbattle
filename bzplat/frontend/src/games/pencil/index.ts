/** 点格棋前端视图规格（封装现有 components/pencil/）。 */
import { Circle } from 'lucide-react'
import type { GameViewSpec } from '../base'
import PencilBoard from '@/components/pencil/PencilBoard'
import { reducePencilEvents } from '@/components/pencil/usePencilState'

export const pencilSpec: GameViewSpec = {
  id: 'pencil',
  label: '点格棋',
  icon: Circle,
  kind: 'board',
  Board: PencilBoard as unknown as GameViewSpec['Board'],
  reduce: reducePencilEvents as unknown as GameViewSpec['reduce'],
  defaultMatchConfig: { n_dots: 11 },
  configFields: [
    { key: 'n_dots', label: '点阵边长', default: 11, min: 3, max: 15 },
  ],
}
