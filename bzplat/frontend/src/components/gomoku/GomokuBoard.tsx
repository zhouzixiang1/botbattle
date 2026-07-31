import type { GomokuViewModel } from './useGomokuState'

export default function GomokuBoard({
  vm,
  onMove,
}: {
  vm: GomokuViewModel
  onMove?: (x: number, y: number) => void
}) {
  const size = vm.size
  const cell = Math.min(28, Math.floor(420 / size))
  const boardPx = cell * size
  const interactive = !!onMove && !vm.matchOver

  return (
    <div className="overflow-hidden rounded-2xl border border-amber-900/30 bg-gradient-to-br from-amber-100 via-amber-50 to-stone-100 p-4 shadow-soft">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-sm text-stone-700">
        <span>
          五子棋 · {size}×{size}
          {vm.moveCount > 0 ? ` · 第 ${vm.moveCount} 手` : ''}
        </span>
        <span>
          {vm.matchOver
            ? vm.winner === null
              ? '平局'
              : `${vm.winner === 0 ? '黑' : '白'}胜${vm.reason ? `（${vm.reason}）` : ''}`
            : `待行：${vm.toAct === 0 ? '黑' : vm.toAct === 1 ? '白' : '—'}`}
        </span>
      </div>
      <div className="mx-auto overflow-auto">
        <svg
          width={boardPx}
          height={boardPx}
          viewBox={`0 0 ${boardPx} ${boardPx}`}
          className="mx-auto block rounded bg-[#e8c98a] shadow-inner"
          role="img"
          aria-label="五子棋棋盘"
        >
          {Array.from({ length: size }).map((_, i) => (
            <g key={`grid-${i}`}>
              <line
                x1={cell / 2}
                y1={cell / 2 + i * cell}
                x2={boardPx - cell / 2}
                y2={cell / 2 + i * cell}
                stroke="#8b6914"
                strokeWidth={1}
              />
              <line
                x1={cell / 2 + i * cell}
                y1={cell / 2}
                x2={cell / 2 + i * cell}
                y2={boardPx - cell / 2}
                stroke="#8b6914"
                strokeWidth={1}
              />
            </g>
          ))}
          {vm.board.map((col, x) =>
            col.map((v, y) => {
              if (v < 0) return null
              const cx = cell / 2 + x * cell
              const cy = cell / 2 + y * cell
              const isLast = vm.lastMove?.x === x && vm.lastMove?.y === y
              return (
                <g key={`s-${x}-${y}`}>
                  <circle
                    cx={cx}
                    cy={cy}
                    r={cell * 0.38}
                    fill={v === 0 ? '#1a1a1a' : '#f5f5f5'}
                    stroke={v === 0 ? '#000' : '#888'}
                    strokeWidth={1}
                  />
                  {isLast && (
                    <circle cx={cx} cy={cy} r={3} fill={v === 0 ? '#f59e0b' : '#dc2626'} />
                  )}
                </g>
              )
            }),
          )}
          {/* 人类落子点击层（每个交叉点一个透明方块） */}
          {interactive &&
            Array.from({ length: size }).map((_, x) =>
              Array.from({ length: size }).map((_, y) => (
                <rect
                  key={`hit-${x}-${y}`}
                  x={x * cell}
                  y={y * cell}
                  width={cell}
                  height={cell}
                  fill="transparent"
                  className="cursor-pointer"
                  onClick={() => onMove!(x, y)}
                />
              )),
            )}
        </svg>
      </div>
      <div className="mt-3 flex gap-4 text-xs text-stone-600">
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 rounded-full bg-stone-900" /> 黑 (0)
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 rounded-full border border-stone-400 bg-white" /> 白 (1)
        </span>
      </div>
    </div>
  )
}
