import {
  GRID_DOT,
  GRID_EDGE,
  GRID_EDGE_USED,
  GRID_BOX,
  type PencilViewModel,
} from './usePencilState'

/** 玩家配色：已占边按玩家着色（红 #ef4444 / 蓝 #3b82f6）。 */
const EDGE_COLOR = ['#ef4444', '#3b82f6']
/** 格归属配色（淡填充）：红 rgba(239,68,68,0.25) / 蓝 rgba(59,130,246,0.25)。 */
const BOX_FILL = ['rgba(239,68,68,0.25)', 'rgba(59,130,246,0.25)']

export default function PencilBoard({
  vm,
  onMove,
}: {
  vm: PencilViewModel
  onMove?: (x: number, y: number) => void
}) {
  const size = vm.size
  const cell = Math.min(36, Math.floor(480 / size))
  const boardPx = cell * size
  const interactive = !!onMove && !vm.matchOver

  return (
    <div className="overflow-hidden rounded-2xl border border-slate-300 bg-gradient-to-br from-slate-50 via-sky-50 to-slate-100 p-4 shadow-soft">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-sm text-slate-700">
        <span>
          点格棋 · {vm.nDots}×{vm.nDots} 点
          {vm.moveCount > 0 ? ` · ${vm.moveCount} 边` : ''}
        </span>
        <span className="font-medium">
          <span className="text-red-600">红 {vm.scores[0]}</span>
          {' : '}
          <span className="text-blue-600">{vm.scores[1]} 蓝</span>
          {vm.matchOver
            ? ` · ${
                vm.winner === null
                  ? '平局'
                  : `${vm.winner === 0 ? '红' : '蓝'}胜（${vm.reason}）`
              }`
            : ` · 待行：${vm.toAct === 0 ? '红' : vm.toAct === 1 ? '蓝' : '—'}`}
          {vm.extraTurn && !vm.matchOver && (
            <span className="ml-1 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
              连走
            </span>
          )}
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

            // 格心：按归属着色（已占格填淡红/淡蓝）
            if (v === GRID_BOX) {
              const owner = vm.boxOwner[x]?.[y]
              if (owner === 0 || owner === 1) {
                // 已占格：填充归属色，画在格心区域（从四角点构成的方块）
                return (
                  <rect
                    key={`b-${x}-${y}`}
                    x={cx - cell / 2}
                    y={cy - cell / 2}
                    width={cell}
                    height={cell}
                    fill={BOX_FILL[owner]}
                  />
                )
              }
              return null
            }

            if (v === GRID_DOT) {
              return (
                <circle
                  key={`d-${x}-${y}`}
                  cx={cx}
                  cy={cy}
                  r={Math.max(2, cell * 0.18)}
                  fill="#334155"
                />
              )
            }
            if (v === GRID_EDGE || v === GRID_EDGE_USED) {
              const horiz = y % 2 === 1 && x % 2 === 0
              const used = v === GRID_EDGE_USED
              // 已占边按玩家着色（经 edgeOwner 取色）；最后一手加粗高亮
              const owner = vm.edgeOwner[`${x},${y}`]
              const baseColor = owner === 0 || owner === 1 ? EDGE_COLOR[owner] : '#0f172a'
              const color = used ? baseColor : '#cbd5e1'
              const sw = used ? (isLast ? Math.max(3, cell * 0.22) : Math.max(2, cell * 0.16)) : Math.max(1, cell * 0.08)
              const clickProps =
                interactive && !used
                  ? {
                      className: 'cursor-pointer',
                      onClick: () => onMove!(x, y),
                      stroke: '#94a3b8' as const,
                      strokeWidth: Math.max(3, cell * 0.22) as number,
                    }
                  : {}
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
                    {...clickProps}
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
                  {...clickProps}
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
