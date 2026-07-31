import {
  GRID_DOT,
  GRID_EDGE,
  GRID_EDGE_USED,
  type PencilViewModel,
} from './usePencilState'

export default function PencilBoard({ vm }: { vm: PencilViewModel }) {
  const size = vm.size
  const cell = Math.min(18, Math.floor(480 / size))
  const boardPx = cell * size

  return (
    <div className="overflow-hidden rounded-2xl border border-slate-300 bg-gradient-to-br from-slate-50 via-sky-50 to-slate-100 p-4 shadow-soft">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-sm text-slate-700">
        <span>
          点格棋 · {vm.nDots}×{vm.nDots} 点
          {vm.moveCount > 0 ? ` · ${vm.moveCount} 边` : ''}
        </span>
        <span className="font-medium">
          红 {vm.scores[0]} : {vm.scores[1]} 蓝
          {vm.matchOver
            ? ` · ${
                vm.winner === null
                  ? '平局'
                  : `${vm.winner === 0 ? '红' : '蓝'}胜`
              }`
            : ` · 待行：${vm.toAct === 0 ? '红' : vm.toAct === 1 ? '蓝' : '—'}`}
        </span>
      </div>
      <svg
        width={boardPx}
        height={boardPx}
        viewBox={`0 0 ${boardPx} ${boardPx}`}
        className="mx-auto block rounded bg-white/80"
        role="img"
        aria-label="点格棋棋盘"
      >
        {vm.grid.map((col, x) =>
          col.map((v, y) => {
            const cx = cell / 2 + x * cell
            const cy = cell / 2 + y * cell
            const isLast = vm.lastEdge?.x === x && vm.lastEdge?.y === y
            if (v === GRID_DOT) {
              return (
                <circle
                  key={`d-${x}-${y}`}
                  cx={cx}
                  cy={cy}
                  r={Math.max(2, cell * 0.22)}
                  fill="#334155"
                />
              )
            }
            if (v === GRID_EDGE || v === GRID_EDGE_USED) {
              const horiz = y % 2 === 1 && x % 2 === 0
              const used = v === GRID_EDGE_USED
              const color = used ? (isLast ? '#ea580c' : '#0f172a') : '#cbd5e1'
              const sw = used ? Math.max(2, cell * 0.18) : Math.max(1, cell * 0.08)
              if (horiz) {
                return (
                  <line
                    key={`e-${x}-${y}`}
                    x1={cx - cell / 2}
                    y1={cy}
                    x2={cx + cell / 2}
                    y2={cy}
                    stroke={color}
                    strokeWidth={sw}
                    strokeLinecap="round"
                  />
                )
              }
              return (
                <line
                  key={`e-${x}-${y}`}
                  x1={cx}
                  y1={cy - cell / 2}
                  x2={cx}
                  y2={cy + cell / 2}
                  stroke={color}
                  strokeWidth={sw}
                  strokeLinecap="round"
                />
              )
            }
            return null
          }),
        )}
      </svg>
    </div>
  )
}
