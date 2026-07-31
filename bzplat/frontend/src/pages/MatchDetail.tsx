import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import PageStub from '../components/PageStub'
import PokerTable from '../components/poker/PokerTable'
import { reduceEvents, type RawEvent } from '../components/poker/useMatchState'
import { apiGet, errMsg } from '../api'

const SPEEDS = [
  { label: '0.5x', ms: 1400 },
  { label: '1x', ms: 700 },
  { label: '2x', ms: 350 },
  { label: '4x', ms: 175 },
]

const ACTION_LABEL: Record<string, string> = {
  fold: '弃牌', check: '过牌', call: '跟注', raise: '加注', allin: '全押',
}

/** 找到「每手的起始事件索引」，用于逐手跳转与导航器 */
function handBoundaries(events: RawEvent[]): number[] {
  const bounds: number[] = []
  events.forEach((ev, i) => {
    if (ev.type === 'hand_start') bounds.push(i)
  })
  if (events.length) bounds.push(events.length) // 末尾哨兵
  return bounds
}

/** 每手的赢家（从 settle 事件提取），用于导航器绿点 */
function handWinners(events: RawEvent[]): (number[] | null)[] {
  const out: (number[] | null)[] = []
  let cur: number[] | null = null
  for (const ev of events) {
    if (ev.type === 'hand_start') {
      if (out.length === 0 || cur !== null) out.push(cur)
      cur = null
    } else if (ev.type === 'settle') {
      cur = (ev.winners as number[] | undefined) ?? null
    }
  }
  if (out.length === 0 || cur !== null) out.push(cur)
  return out
}

export default function MatchDetail() {
  const { id } = useParams()
  const [data, setData] = useState<{
    match: Record<string, unknown>
    replay: { events_json?: string }
  } | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [stepIdx, setStepIdx] = useState(-1) // -1 = 末尾（最新）
  const [playing, setPlaying] = useState(false)
  const [speedIdx, setSpeedIdx] = useState(1)

  useEffect(() => {
    if (!id) return
    setLoading(true)
    apiGet<{ match: Record<string, unknown>; replay: { events_json?: string } }>(
      `/api/matches/${encodeURIComponent(id)}`,
    )
      .then((d) => {
        setData(d)
        setError('')
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false))
  }, [id])

  const events = useMemo<RawEvent[]>(() => {
    try {
      return JSON.parse(data?.replay?.events_json || '[]') as RawEvent[]
    } catch {
      return []
    }
  }, [data])

  const bounds = useMemo(() => handBoundaries(events), [events])
  const winners = useMemo(() => handWinners(events), [events])
  const total = events.length
  // 真实显示到第几步（-1 表示全部/最新）
  const cur = stepIdx < 0 ? total - 1 : Math.min(stepIdx, total - 1)
  const visible = cur >= 0 ? events.slice(0, cur + 1) : []
  const vm = useMemo(() => reduceEvents(visible), [visible])
  vm.status = data?.match ? String(data.match.status) : 'idle'

  // 自动播放
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (!playing || total === 0) return
    if (cur >= total - 1) {
      setPlaying(false)
      return
    }
    timerRef.current = setTimeout(() => {
      setStepIdx((s) => {
        const next = (s < 0 ? total - 1 : s) + 1
        return next >= total - 1 ? -1 : next
      })
    }, SPEEDS[speedIdx].ms)
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [playing, cur, total, speedIdx])

  // 数据到达后默认定位到末尾
  useEffect(() => {
    if (total > 0 && stepIdx === -1) setStepIdx(total - 1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [total])

  // 手动操作时暂停
  const pause = () => setPlaying(false)

  const jumpHand = (delta: number) => {
    pause()
    if (!bounds.length) return
    let hIdx = 0
    for (let i = 0; i < bounds.length - 1; i++) {
      if (cur >= bounds[i] && cur < bounds[i + 1]) {
        hIdx = i
        break
      }
    }
    const target = Math.max(0, Math.min(bounds.length - 2, hIdx + delta))
    setStepIdx(bounds[target] ?? 0)
  }

  const jumpToHand = (hIdx: number) => {
    pause()
    setStepIdx(bounds[hIdx] ?? 0)
  }

  // 动作列表自动滚动到当前行（仅滚动列表容器内部，不影响外层页面位置）
  const actionListRef = useRef<HTMLDivElement>(null)
  const curActionRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const container = actionListRef.current
    const row = curActionRef.current
    if (!container || !row) return
    const cTop = container.scrollTop
    const cBottom = cTop + container.clientHeight
    const rTop = row.offsetTop
    const rBottom = rTop + row.offsetHeight
    if (rTop < cTop) container.scrollTop = rTop
    else if (rBottom > cBottom) container.scrollTop = rBottom - container.clientHeight
  }, [cur])

  const match = data?.match
  const isLive = match?.status === 'running' || match?.status === 'pending'
  // 当前手号（用于导航器高亮）
  const curHandIdx = (() => {
    for (let i = 0; i < bounds.length - 1; i++) {
      if (cur >= bounds[i] && cur < bounds[i + 1]) return i
    }
    return bounds.length >= 2 ? bounds.length - 2 : 0
  })()

  return (
    <PageStub title="对局详情">
      <p className="font-mono text-xs text-slate-500">{id}</p>
      {error && <p className="mt-4 text-sm text-error-500">{error}</p>}

      {match && (
        <div className="mt-4 grid gap-2 card p-4 text-sm text-slate-600 sm:grid-cols-3">
          <div>状态：{String(match.status)}</div>
          <div>
            手数：{String(match.hands_played)}/{String(match.total_hands)}
          </div>
          <div>类型：{String(match.match_type)}</div>
          <div>A 净筹码：{String(match.earnings_a)}</div>
          <div>B 净筹码：{String(match.earnings_b)}</div>
          <div>
            胜者：{match.winner == null ? '—' : `座位 ${String(match.winner)}`}
          </div>
          {isLive && (
            <Link className="text-brand-600 hover:underline" to={`/watch/${id}`}>
              实时观赛 →
            </Link>
          )}
        </div>
      )}

      {/* 扑克桌 */}
      <div className="mt-6">
        {loading ? (
          <p className="py-12 text-center text-sm text-slate-400">加载回放…</p>
        ) : visible.length === 0 ? (
          <p className="py-12 text-center text-sm text-slate-500">
            {isLive ? (
              <>
                对局进行中，暂无完整回放。{' '}
                <Link to={`/watch/${id}`} className="text-brand-600 hover:underline">
                  去观赛 →
                </Link>
              </>
            ) : events.length === 0 ? (
              '暂无回放数据'
            ) : (
              ''
            )}
          </p>
        ) : (
          <PokerTable vm={vm} revealMode="all" />
        )}
      </div>

      {/* 手导航器（点格子跳手） */}
      {bounds.length >= 2 && (
        <div className="mx-auto mt-4 w-full max-w-2xl">
          <div className="mb-1 text-[10px] text-slate-400">手导航（点击跳转）</div>
          <div className="flex flex-wrap gap-1 rounded-lg border border-slate-200 bg-white p-2">
            {Array.from({ length: bounds.length - 1 }, (_, h) => {
              const ws = winners[h]
              const isCur = h === curHandIdx
              return (
                <button
                  key={h}
                  type="button"
                  onClick={() => jumpToHand(h)}
                  title={`第 ${h + 1} 手${ws ? `：胜者座位 ${ws.join('/')}` : ''}`}
                  className={`relative h-7 w-7 rounded text-[10px] font-medium transition ${
                    isCur
                      ? 'bg-brand-600 text-white'
                      : 'bg-slate-100 text-slate-500 hover:bg-slate-200'
                  }`}
                >
                  {h + 1}
                  {/* 赢家绿点指示 */}
                  {ws && !isCur && (
                    <span className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full bg-success-500" />
                  )}
                </button>
              )
            })}
          </div>
        </div>
      )}

      {/* 回放控制条 */}
      {total > 0 && (
        <div className="mx-auto mt-3 w-full max-w-2xl rounded-xl border border-slate-200 bg-white p-3">
          <div className="flex flex-wrap items-center justify-center gap-2">
            <button
              type="button"
              onClick={() => jumpHand(-1)}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-100"
            >
              ⏮ 上一手
            </button>
            <button
              type="button"
              onClick={() => {
                pause()
                setStepIdx((s) => Math.max(0, (s < 0 ? total - 1 : s) - 1))
              }}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-100"
            >
              ◀ 上一步
            </button>
            <button
              type="button"
              onClick={() => {
                if (cur >= total - 1) {
                  setStepIdx(0)
                }
                setPlaying((p) => !p)
              }}
              className="rounded-lg bg-brand-600 px-4 py-1.5 text-xs font-medium text-white hover:bg-brand-500"
            >
              {playing ? '❚❚ 暂停' : '▶ 播放'}
            </button>
            <button
              type="button"
              onClick={() => {
                pause()
                setStepIdx((s) => Math.min(total - 1, (s < 0 ? total - 1 : s) + 1))
              }}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-100"
            >
              下一步 ▶
            </button>
            <button
              type="button"
              onClick={() => jumpHand(1)}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-100"
            >
              下一手 ⏭
            </button>
            <select
              value={speedIdx}
              onChange={(e) => setSpeedIdx(Number(e.target.value))}
              className="rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-700"
            >
              {SPEEDS.map((s, i) => (
                <option key={i} value={i}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>
          {/* 进度条 */}
          <div className="mt-3 flex items-center gap-2">
            <span className="text-[10px] text-slate-500">
              步 {cur + 1}/{total}
            </span>
            <input
              type="range"
              min={0}
              max={Math.max(0, total - 1)}
              value={cur}
              onChange={(e) => {
                pause()
                setStepIdx(Number(e.target.value))
              }}
              className="flex-1 accent-brand-500"
            />
          </div>
        </div>
      )}

      {/* 动作列表（自动滚动 + 高亮当前） */}
      {visible.length > 0 && (
        <div className="mx-auto mt-4 w-full max-w-2xl">
          <div className="mb-1 text-[10px] text-slate-400">动作时序</div>
          <div ref={actionListRef} className="max-h-64 overflow-y-auto rounded-xl border border-slate-200 bg-white p-2 text-xs">
            {visible.map((ev, i) => (
              <div
                key={i}
                ref={i === cur ? curActionRef : undefined}
                className={`flex items-center gap-2 rounded px-2 py-1 ${
                  i === cur ? 'bg-brand-50 font-medium text-brand-700' : 'text-slate-600'
                }`}
              >
                <span className="w-8 font-mono text-slate-400">{i + 1}</span>
                <span className="w-14 text-slate-500">{ev.type}</span>
                <span className="flex-1 truncate text-slate-500">
                  {ev.type === 'action'
                    ? `座位 ${ev.player} · ${ACTION_LABEL[String(ev.action)] ?? ev.action}${
                        ev.amount ? ' ' + String(ev.amount) : ''
                      }`
                    : ev.type === 'settle'
                      ? `赢家 座位 ${(ev.winners as number[] | undefined)?.join('/') ?? '?'} · 底池 ${ev.pot}`
                      : ev.type === 'hand_start'
                        ? `第 ${(Number(ev.hand) || 0) + 1} 手开始`
                        : ev.type === 'deal_board'
                          ? `${ev.street}：${(ev.dealt as string[] | undefined)?.join(' ')}`
                          : JSON.stringify(ev).slice(0, 60)}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <Link to="/" className="mt-6 inline-block text-sm text-brand-600 hover:text-brand-700">
        ← 返回
      </Link>
    </PageStub>
  )
}
