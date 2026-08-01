import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { apiGet, apiJson, errMsg } from '../api'
import { useAuth } from '../components/useAuth'
import { gameLabel } from '../lib/games'
import { tierFor } from '../lib/tiers'

function TierInline({ rating, name }: { rating: number | null | undefined; name?: string }) {
  const t = tierFor(rating)
  return (
    <span className={`mt-1 inline-block rounded-full px-2 py-0.5 text-[10px] font-medium ${t.bg} ${t.color}`}>
      {name || t.name}
    </span>
  )
}

/* ── 类型 ─────────────────────────────────────────────── */
interface BotProfile {
  id: number
  name: string
  display_name: string
  description?: string
  game_id: string
  owner_id: number
  owner_name?: string
  owner_display?: string
  is_active: number | boolean
  is_public: number | boolean
  format?: string
  os?: string
  arch?: string
  current_version?: number
  created_at?: string
  rating?: number
  rd?: number
  vol?: number
  wins?: number
  losses?: number
  draws?: number
  net_chips?: number
  matches_played?: number
  rated_at?: string
  tier_level?: number
  tier_key?: string
  tier_name?: string
}

interface MatchRow {
  id: string
  game_id: string
  status: string
  winner: number | null
  match_type: string
  bot_a_id: number
  bot_b_id: number
  bot_a_name: string
  bot_b_name: string
  bot_a_display?: string
  bot_b_display?: string
  earnings_a?: number
  earnings_b?: number
  hands_played?: number
  created_at?: string
}

interface OpponentRow {
  opponent_id: number
  opponent_name: string
  opponent_display?: string
  game_id?: string
  wins: number
  losses: number
  draws: number
  samples: number
  last_played_at?: string
}

interface RatingPoint {
  id: number
  rating: number
  rd: number
  vol: number
  matches_played: number
  reason?: string
  created_at: string
}

/* ── 辅助 ─────────────────────────────────────────────── */
function winRate(p?: BotProfile): number {
  const w = p?.wins ?? 0
  const l = p?.losses ?? 0
  const d = p?.draws ?? 0
  const total = w + l + d
  if (total === 0) return 0
  return (w + d * 0.5) / total
}

function fmtPct(r: number): string {
  return `${(r * 100).toFixed(1)}%`
}

function matchOutcome(m: MatchRow, botId: number): 'win' | 'loss' | 'draw' | '' {
  if (m.status !== 'completed') return ''
  if (m.winner === null) return 'draw'
  const isA = m.bot_a_id === botId
  const won = (isA && m.winner === 0) || (!isA && m.winner === 1)
  return won ? 'win' : 'loss'
}

function RatingChart({ points }: { points: RatingPoint[] }) {
  if (points.length < 2) {
    return <div className="py-6 text-center text-xs text-slate-400">评分数据不足，至少需 2 个数据点</div>
  }
  const W = 520
  const H = 140
  const pad = 28
  const ratings = points.map((p) => p.rating)
  const lo = Math.min(...ratings)
  const hi = Math.max(...ratings)
  const span = Math.max(50, hi - lo) // 至少 50 区间
  const yLo = Math.floor(lo - span * 0.1)
  const yHi = Math.ceil(hi + span * 0.1)
  const xStep = (W - pad * 2) / Math.max(1, points.length - 1)
  const y = (v: number) => H - pad - ((v - yLo) / (yHi - yLo)) * (H - pad * 2)
  const x = (i: number) => pad + i * xStep
  const path = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i)},${y(p.rating)}`).join(' ')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="评分变化曲线">
      {/* 网格基线 */}
      {[0, 0.5, 1].map((f) => (
        <line
          key={f}
          x1={pad}
          x2={W - pad}
          y1={pad + f * (H - pad * 2)}
          y2={pad + f * (H - pad * 2)}
          stroke="#e2e8f0"
          strokeWidth={1}
        />
      ))}
      {/* Y 轴标注 */}
      <text x={4} y={pad + 4} className="fill-slate-400" fontSize={10}>
        {yHi.toFixed(0)}
      </text>
      <text x={4} y={H - pad + 4} className="fill-slate-400" fontSize={10}>
        {yLo.toFixed(0)}
      </text>
      {/* 曲线 */}
      <path d={path} fill="none" stroke="#0ea5e9" strokeWidth={2} />
      {/* 数据点 */}
      {points.map((p, i) => (
        <circle key={p.id} cx={x(i)} cy={y(p.rating)} r={2.5} fill="#0284c7" />
      ))}
      {/* 首尾 rating 标注 */}
      <text x={x(0)} y={y(points[0].rating) - 8} className="fill-slate-500" fontSize={10} textAnchor="middle">
        {points[0].rating.toFixed(0)}
      </text>
      <text
        x={x(points.length - 1)}
        y={y(points[points.length - 1].rating) - 8}
        className="fill-slate-500"
        fontSize={10}
        textAnchor="middle"
      >
        {points[points.length - 1].rating.toFixed(0)}
      </text>
    </svg>
  )
}

/* ── 主组件 ───────────────────────────────────────────── */
export default function BotDetail() {
  const { id } = useParams<{ id: string }>()
  const botId = Number(id)
  const { user } = useAuth()
  const [profile, setProfile] = useState<BotProfile | null>(null)
  const [matches, setMatches] = useState<MatchRow[]>([])
  const [opponents, setOpponents] = useState<OpponentRow[]>([])
  const [history, setHistory] = useState<RatingPoint[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<'history' | 'opponents' | 'rating'>('history')
  const [favorited, setFavorited] = useState(false)
  const [favCount, setFavCount] = useState(0)

  useEffect(() => {
    if (!botId) return
    setLoading(true)
    Promise.all([
      apiGet<{ profile: BotProfile }>(`/api/bots/${botId}/profile`),
      apiGet<{ matches: MatchRow[] }>(`/api/bots/${botId}/matches?limit=30`),
      apiGet<{ opponents: OpponentRow[] }>(`/api/bots/${botId}/opponents?limit=20`),
      apiGet<{ history: RatingPoint[] }>(`/api/bots/${botId}/rating-history?limit=100`),
    ])
      .then(([p, m, o, h]) => {
        setProfile(p.profile)
        setMatches(m.matches || [])
        setOpponents(o.opponents || [])
        setHistory(h.history || [])
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false))
    // 收藏状态（登录后）
    if (user) {
      apiGet<{ favorited: boolean; favorite_count: number }>(
        `/api/bots/${botId}/favorite-status`,
      )
        .then((fs) => {
          setFavorited(fs.favorited)
          setFavCount(fs.favorite_count)
        })
        .catch(() => {})
    }
  }, [botId, user])

  function toggleFavorite() {
    if (!user) return
    const method = favorited ? 'DELETE' : 'POST'
    apiJson(`/api/bots/${botId}/favorite`, method)
      .then(() => {
        setFavorited(!favorited)
        setFavCount((c) => c + (favorited ? -1 : 1))
      })
      .catch((e) => setError(errMsg(e)))
  }

  if (loading) {
    return (
      <PageStub title="Bot 详情">
        <p className="py-8 text-center text-sm text-slate-400">加载中…</p>
      </PageStub>
    )
  }
  if (error || !profile) {
    return (
      <PageStub title="Bot 详情">
        <p className="mb-3 text-sm text-error-500">{error || 'Bot 不存在'}</p>
        <Link to="/leaderboard" className="text-sm text-brand-600 hover:text-brand-700">
          ← 返回排行榜
        </Link>
      </PageStub>
    )
  }

  const wr = winRate(profile)
  const total = (profile.wins ?? 0) + (profile.losses ?? 0) + (profile.draws ?? 0)

  return (
    <PageStub title={profile.display_name || profile.name}>
      {/* 顶部信息卡 */}
      <div className="card mb-5 p-5">
        <div className="flex flex-wrap items-start gap-4">
          <div className="min-w-[16rem] flex-1">
            <h2 className="text-xl font-bold text-slate-900">
              {profile.display_name || profile.name}
            </h2>
            <p className="text-sm text-slate-500">@{profile.name}</p>
            <p className="mt-1 text-sm text-slate-500">
              游戏：
              <span className="ml-1 inline-block rounded-full bg-brand-50 px-2 py-0.5 text-xs font-medium text-brand-700">
                {gameLabel(profile.game_id)}
              </span>
            </p>
            {profile.description && (
              <p className="mt-2 text-sm text-slate-600">{profile.description}</p>
            )}
            <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-400">
              <span>
                所有者：
                {profile.owner_name ? (
                  <Link
                    to={`/user/${encodeURIComponent(profile.owner_name)}`}
                    className="text-brand-600 hover:text-brand-700"
                  >
                    {profile.owner_display || profile.owner_name}
                  </Link>
                ) : (
                  '—'
                )}
              </span>
              <span>版本 v{profile.current_version ?? 1}</span>
              <span>
                {profile.format}/{profile.os}-{profile.arch}
              </span>
              {profile.created_at && <span>创建于 {profile.created_at.slice(0, 10)}</span>}
              {profile.is_active ? null : (
                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-500">已停用</span>
              )}
            </div>
            {user && (
              <button
                type="button"
                onClick={toggleFavorite}
                className={`mt-2 rounded-lg px-3 py-1.5 text-xs font-medium ${
                  favorited
                    ? 'border border-slate-300 bg-white text-slate-600 hover:bg-slate-50'
                    : 'bg-brand-600 text-white hover:bg-brand-500'
                }`}
              >
                {favorited ? '★ 已收藏' : '☆ 收藏'}（{favCount}）
              </button>
            )}
          </div>

          {/* 评分/战绩 */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-center">
              <div className="text-xs uppercase tracking-wide text-slate-400">Rating / 段位</div>
              <div className="mt-1 font-mono text-2xl font-bold text-brand-700">
                {profile.rating != null ? Number(profile.rating).toFixed(0) : '—'}
              </div>
              {profile.tier_name && (
                <TierInline rating={profile.rating} name={profile.tier_name} />
              )}
              {profile.rd != null && (
                <div className="text-[10px] text-slate-400">rd {Number(profile.rd).toFixed(0)}</div>
              )}
            </div>
            <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-center">
              <div className="text-xs uppercase tracking-wide text-slate-400">胜率</div>
              <div className="mt-1 font-mono text-2xl font-bold text-slate-800">{fmtPct(wr)}</div>
              <div className="text-[10px] text-slate-400">共 {total} 场</div>
            </div>
            <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-center">
              <div className="text-xs uppercase tracking-wide text-slate-400">胜</div>
              <div className="mt-1 font-mono text-2xl font-bold text-success-600">
                {profile.wins ?? 0}
              </div>
            </div>
            <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-center">
              <div className="text-xs uppercase tracking-wide text-slate-400">负/平</div>
              <div className="mt-1 font-mono text-2xl font-bold text-error-600">
                {profile.losses ?? 0}
                <span className="text-base text-slate-400"> / {profile.draws ?? 0}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="mb-3 flex gap-1 border-b border-slate-200">
        {(
          [
            ['history', `对局历史 (${matches.length})`],
            ['opponents', `对手战绩 (${opponents.length})`],
            ['rating', '评分曲线'],
          ] as const
        ).map(([k, label]) => (
          <button
            key={k}
            type="button"
            onClick={() => setTab(k)}
            className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium ${
              tab === k
                ? 'border-brand-500 text-brand-700'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* 对局历史 */}
      {tab === 'history' && (
        <div className="overflow-x-auto rounded-xl border border-slate-200">
          <table className="w-full min-w-[34rem] text-left text-sm">
            <thead className="bg-white text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-3 py-2.5">时间</th>
                <th className="px-3 py-2.5">对手</th>
                <th className="px-3 py-2.5">结果</th>
                <th className="px-3 py-2.5">类型</th>
                <th className="px-3 py-2.5">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {matches.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-3 py-8 text-center text-slate-400">
                    暂无对局
                  </td>
                </tr>
              ) : (
                matches.map((m) => {
                  const isA = m.bot_a_id === botId
                  const oppName = isA ? m.bot_b_display || m.bot_b_name : m.bot_a_display || m.bot_a_name
                  const oppId = isA ? m.bot_b_id : m.bot_a_id
                  const outcome = matchOutcome(m, botId)
                  return (
                    <tr key={m.id} className="bg-white hover:bg-slate-100/60">
                      <td className="px-3 py-2.5 text-xs text-slate-400">
                        {m.created_at?.slice(0, 16).replace('T', ' ') || '—'}
                      </td>
                      <td className="px-3 py-2.5">
                        <Link to={`/bot/${oppId}`} className="text-slate-700 hover:text-brand-600">
                          {oppName || `#${oppId}`}
                        </Link>
                      </td>
                      <td className="px-3 py-2.5">
                        {m.status === 'completed' ? (
                          <span
                            className={
                              outcome === 'win'
                                ? 'font-medium text-success-600'
                                : outcome === 'loss'
                                  ? 'font-medium text-error-600'
                                  : 'text-slate-500'
                            }
                          >
                            {outcome === 'win' ? '胜' : outcome === 'loss' ? '负' : '平'}
                          </span>
                        ) : (
                          <span className="text-slate-400">{m.status}</span>
                        )}
                      </td>
                      <td className="px-3 py-2.5 text-xs text-slate-400">{m.match_type}</td>
                      <td className="px-3 py-2.5">
                        <Link
                          to={`/match/${encodeURIComponent(m.id)}`}
                          className="text-brand-600 hover:text-brand-700"
                        >
                          回放 →
                        </Link>
                      </td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* 对手战绩 */}
      {tab === 'opponents' && (
        <div className="overflow-x-auto rounded-xl border border-slate-200">
          <table className="w-full min-w-[30rem] text-left text-sm">
            <thead className="bg-white text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-3 py-2.5">对手</th>
                <th className="px-3 py-2.5">交手</th>
                <th className="px-3 py-2.5">胜</th>
                <th className="px-3 py-2.5">负</th>
                <th className="px-3 py-2.5">平</th>
                <th className="px-3 py-2.5">胜率</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {opponents.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-3 py-8 text-center text-slate-400">
                    暂无对手战绩
                  </td>
                </tr>
              ) : (
                opponents.map((o) => {
                  const t = o.wins + o.losses + o.draws
                  const r = t > 0 ? (o.wins + o.draws * 0.5) / t : 0
                  return (
                    <tr key={o.opponent_id} className="bg-white hover:bg-slate-100/60">
                      <td className="px-3 py-2.5">
                        <Link
                          to={`/bot/${o.opponent_id}`}
                          className="text-slate-700 hover:text-brand-600"
                        >
                          {o.opponent_display || o.opponent_name || `#${o.opponent_id}`}
                        </Link>
                      </td>
                      <td className="px-3 py-2.5 text-slate-500">{o.samples || t}</td>
                      <td className="px-3 py-2.5 text-success-600">{o.wins}</td>
                      <td className="px-3 py-2.5 text-error-600">{o.losses}</td>
                      <td className="px-3 py-2.5 text-slate-400">{o.draws}</td>
                      <td className="px-3 py-2.5 font-mono text-slate-600">{fmtPct(r)}</td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* 评分曲线 */}
      {tab === 'rating' && (
        <div className="card p-4">
          <p className="mb-2 text-sm text-slate-500">
            评分变化时序（Glicko-2，共 {history.length} 个数据点）
          </p>
          <RatingChart points={history} />
        </div>
      )}
    </PageStub>
  )
}
