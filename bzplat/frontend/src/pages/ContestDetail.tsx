import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { useAuth } from '../components/useAuth'
import { apiGet, apiJson, errMsg } from '../api'

interface Contest {
  id: number
  title: string
  description?: string
  status: string
  organizer_id: number
  hands_per_match?: number
  template_id?: string
  game_id?: string
  stages_json?: string
  current_stage_idx?: number
  rest_ends_at?: string | null
}

interface Stage {
  key?: string
  type?: string
  scoring?: string
  rounds?: number
  group_count?: number
  advance_count?: number
  advance_per_group?: number
  rest_after_minutes?: number
  allow_bot_swap_in_rest?: boolean
}

interface Entry {
  id: number
  user_id: number
  bot_id: number
  registered_at?: string
  group_id?: string
  seed?: number
  eliminated?: number
  bot_name?: string
  bot_display?: string
  owner_name?: string
  owner_display?: string
}

interface Pairing {
  id: number
  round_num?: number
  bot_a_id: number
  bot_b_id: number
  match_id?: string | null
  status?: string
  stage_idx?: number
  stage_key?: string
  group_id?: string
  bot_a_name?: string
  bot_a_display?: string
  bot_b_name?: string
  bot_b_display?: string
  owner_a_name?: string
  owner_b_name?: string
  match_winner?: number | null
}

interface Standing {
  bot_id: number
  points: number
  wins: number
  draws: number
  losses: number
  net_chips: number
  group_id?: string
  bot_name?: string
}

function parseStages(c: Contest | null): Stage[] {
  if (!c?.stages_json) return []
  try {
    return JSON.parse(c.stages_json)
  } catch {
    return []
  }
}

function RestCountdown({ endsAt }: { endsAt: string }) {
  const [left, setLeft] = useState('')
  useEffect(() => {
    const tick = () => {
      const ms = new Date(endsAt).getTime() - Date.now()
      if (ms <= 0) {
        setLeft('已到时')
        return
      }
      const s = Math.floor(ms / 1000)
      const m = Math.floor(s / 60)
      const r = s % 60
      setLeft(`${m}:${r.toString().padStart(2, '0')}`)
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [endsAt])
  return <span className="font-mono text-brand-700">{left}</span>
}

export default function ContestDetail() {
  const { id } = useParams()
  const { user, isLoggedIn } = useAuth()
  const [contest, setContest] = useState<Contest | null>(null)
  const [entries, setEntries] = useState<Entry[]>([])
  const [pairings, setPairings] = useState<Pairing[]>([])
  const [standings, setStandings] = useState<Standing[]>([])
  const [estimate, setEstimate] = useState<{
    estimated_matches?: number
    eta_seconds?: number
  } | null>(null)
  const [bots, setBots] = useState<Array<{ id: number; name: string; display_name?: string }>>(
    [],
  )
  const [botId, setBotId] = useState('')
  const [stageTab, setStageTab] = useState(0)
  const [error, setError] = useState('')

  const stages = useMemo(() => parseStages(contest), [contest])

  const load = useCallback(() => {
    if (!id) return
    apiGet<{
      contest: Contest
      entries: Entry[]
      pairings: Pairing[]
      standings: Standing[]
      estimate?: { estimated_matches?: number; eta_seconds?: number }
    }>(`/api/contests/${id}`)
      .then((d) => {
        setContest(d.contest)
        setEntries(d.entries || [])
        setPairings(d.pairings || [])
        setStandings(d.standings || [])
        setEstimate(d.estimate || null)
        const idx = d.contest.current_stage_idx ?? 0
        setStageTab(idx)
      })
      .catch((e) => setError(errMsg(e)))
  }, [id])

  useEffect(() => {
    void load()
    if (isLoggedIn) {
      apiGet<{ bots: Array<{ id: number; name: string; display_name?: string }> }>(
        '/api/bots/mine',
      )
        .then((d) => setBots(d.bots || []))
        .catch(() => undefined)
    }
  }, [load, isLoggedIn])

  const isOrg =
    !!user &&
    !!contest &&
    (user.role === 'admin' || user.id === contest.organizer_id)

  const myEntry = entries.find((e) => e.user_id === user?.id)

  const act = async (path: string, body?: unknown) => {
    setError('')
    try {
      await apiJson(path, 'POST', body)
      await load()
    } catch (e) {
      setError(errMsg(e))
    }
  }

  const stagePairings = pairings.filter((p) => (p.stage_idx ?? 0) === stageTab)

  if (!contest) {
    return (
      <PageStub title="比赛详情">
        <p className="text-slate-400">{error || '加载中…'}</p>
      </PageStub>
    )
  }

  return (
    <PageStub title="比赛详情">
      <div className="rounded-xl border border-slate-200 bg-white p-4">
        <h2 className="text-lg font-medium text-slate-900">{contest.title}</h2>
        <p className="mt-1 text-sm text-slate-400">{contest.description || '无说明'}</p>
        <p className="mt-2 text-xs text-slate-500">
          状态 <span className="text-brand-700">{contest.status}</span> · 模板{' '}
          {contest.template_id} · 游戏 {contest.game_id || 'holdem'} · 每场{' '}
          {contest.hands_per_match ?? 70} 手
          {estimate?.estimated_matches != null && (
            <> · 预估 {estimate.estimated_matches} 场</>
          )}
        </p>
        {contest.status === 'rest' && contest.rest_ends_at && (
          <p className="mt-2 text-sm text-slate-600">
            阶段休息中，倒计时 <RestCountdown endsAt={contest.rest_ends_at} />
            （可更换派遣 Bot）
          </p>
        )}
      </div>
      {error && <p className="mt-3 text-sm text-error-500">{error}</p>}

      <div className="mt-4 flex flex-wrap gap-2">
        {isOrg && contest.status === 'draft' && (
          <button
            type="button"
            className="rounded-lg bg-brand-600 px-3 py-2 text-sm text-white"
            onClick={() => void act(`/api/contests/${id}/open`)}
          >
            开放报名
          </button>
        )}
        {isOrg && (contest.status === 'open' || contest.status === 'draft') && (
          <button
            type="button"
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-100"
            onClick={() => void act(`/api/contests/${id}/start`)}
          >
            开始比赛
          </button>
        )}
        {isOrg && contest.status === 'rest' && (
          <button
            type="button"
            className="rounded-lg bg-brand-600 px-3 py-2 text-sm text-white"
            onClick={() => void act(`/api/contests/${id}/resume`)}
          >
            结束休息 / 下一阶段
          </button>
        )}
        {isLoggedIn && contest.status === 'open' && (
          <div className="flex flex-wrap items-center gap-2">
            <select
              className="rounded-lg border border-slate-300 bg-white px-2 py-2 text-sm"
              value={botId}
              onChange={(e) => setBotId(e.target.value)}
            >
              <option value="">选择我的 Bot</option>
              {bots.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.display_name || b.name}
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={!botId}
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm hover:bg-white disabled:opacity-40"
              onClick={() =>
                void act(`/api/contests/${id}/register`, { bot_id: Number(botId) })
              }
            >
              报名派遣
            </button>
          </div>
        )}
        {isLoggedIn && myEntry && contest.status === 'rest' && (
          <div className="flex flex-wrap items-center gap-2">
            <select
              className="rounded-lg border border-slate-300 bg-white px-2 py-2 text-sm"
              value={botId}
              onChange={(e) => setBotId(e.target.value)}
            >
              <option value="">更换 Bot（当前 #{myEntry.bot_id}）</option>
              {bots.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.display_name || b.name}
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={!botId}
              className="rounded-lg border border-brand-300 px-3 py-2 text-sm text-brand-700 hover:bg-brand-50 disabled:opacity-40"
              onClick={() =>
                void act(`/api/contests/${id}/dispatch`, { bot_id: Number(botId) })
              }
            >
              确认更换
            </button>
          </div>
        )}
      </div>

      {stages.length > 0 && (
        <>
          <div className="mt-6 flex flex-wrap gap-1 border-b border-slate-200">
            {stages.map((s, i) => (
              <button
                key={s.key || i}
                type="button"
                onClick={() => setStageTab(i)}
                className={`-mb-px border-b-2 px-3 py-2 text-sm ${
                  stageTab === i
                    ? 'border-brand-500 text-brand-700'
                    : 'border-transparent text-slate-500'
                }`}
              >
                {s.key || `阶段${i + 1}`}
                <span className="ml-1 text-xs text-slate-400">({s.type})</span>
                {contest.current_stage_idx === i && contest.status !== 'finished' && (
                  <span className="ml-1 text-xs text-emerald-600">当前</span>
                )}
              </button>
            ))}
          </div>
          {/* 当前阶段配置（只读） */}
          {stages[stageTab] && (
            <div className="mt-2 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-500">
              <span className="font-medium text-slate-600">本阶段配置：</span>
              {[
                stages[stageTab].type,
                stages[stageTab].scoring,
                stages[stageTab].group_count ? `分组=${stages[stageTab].group_count}` : null,
                stages[stageTab].rounds !== undefined ? `轮数=${stages[stageTab].rounds}` : null,
                stages[stageTab].advance_count ? `晋级=${stages[stageTab].advance_count}` : null,
                stages[stageTab].advance_per_group ? `每组晋级=${stages[stageTab].advance_per_group}` : null,
                stages[stageTab].rest_after_minutes ? `休息=${stages[stageTab].rest_after_minutes}分` : null,
                stages[stageTab].allow_bot_swap_in_rest ? '休息可换Bot' : null,
              ]
                .filter(Boolean)
                .join(' · ')}
            </div>
          )}
        </>
      )}

      <h3 className="mt-8 text-sm font-medium text-slate-600">报名</h3>
      <ul className="mt-2 space-y-1 text-sm text-slate-600">
        {entries.length === 0 && <li className="text-slate-400">暂无报名</li>}
        {entries.map((e) => (
          <li key={e.id} className="flex flex-wrap items-center gap-2">
            <Link to={`/bot/${e.bot_id}`} className="font-medium text-slate-800 hover:text-brand-600">
              {e.bot_display || e.bot_name || `#${e.bot_id}`}
            </Link>
            {e.owner_name && (
              <Link to={`/user/${encodeURIComponent(e.owner_name)}`} className="text-xs text-slate-400 hover:text-brand-600">
                @{e.owner_display || e.owner_name}
              </Link>
            )}
            {e.seed ? <span className="text-xs text-slate-400">种子 {e.seed}</span> : ''}
            {e.group_id ? <span className="rounded bg-slate-100 px-1.5 text-xs text-slate-500">{e.group_id}</span> : ''}
            {e.eliminated ? <span className="text-xs text-error-500">[淘汰]</span> : ''}
          </li>
        ))}
      </ul>

      <h3 className="mt-8 text-sm font-medium text-slate-600">积分榜</h3>
      <table className="mt-2 min-w-full text-left text-sm">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-4">#</th>
            <th className="py-1 pr-4">Bot</th>
            <th className="py-1 pr-4">积分</th>
            <th className="py-1 pr-4">W/D/L</th>
            <th className="py-1">净筹码</th>
          </tr>
        </thead>
        <tbody>
          {standings.map((s, i) => (
            <tr key={s.bot_id} className="border-t border-slate-200">
              <td className="py-1 pr-4 text-slate-400">{i + 1}</td>
              <td className="py-1 pr-4">
                <Link to={`/bot/${s.bot_id}`} className="text-slate-800 hover:text-brand-600">
                  {s.bot_name || `#${s.bot_id}`}
                </Link>
              </td>
              <td className="py-1 pr-4 text-brand-700">{s.points}</td>
              <td className="py-1 pr-4">
                {s.wins}/{s.draws}/{s.losses}
              </td>
              <td className="py-1">{s.net_chips}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3 className="mt-8 text-sm font-medium text-slate-600">
        对阵{stages.length ? ` · ${stages[stageTab]?.key || `阶段${stageTab}`}` : ''}
      </h3>
      <ul className="mt-2 space-y-1.5 text-sm text-slate-600">
        {stagePairings.length === 0 && <li className="text-slate-400">暂无对阵</li>}
        {stagePairings.map((p) => {
          const w = p.match_winner
          return (
            <li key={p.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-slate-200 px-3 py-1.5">
              <span className="rounded bg-slate-100 px-1.5 text-xs text-slate-500">R{p.round_num ?? 1}</span>
              {p.group_id && <span className="rounded bg-slate-100 px-1.5 text-xs text-slate-500">{p.group_id}</span>}
              <span className="flex items-center gap-1">
                <Link to={`/bot/${p.bot_a_id}`} className={`hover:text-brand-600 ${w === 0 ? 'font-semibold text-success-600' : w === 1 ? 'text-slate-400' : ''}`}>
                  {p.bot_a_display || p.bot_a_name || `#${p.bot_a_id}`}
                </Link>
                <span className="text-slate-400">vs</span>
                <Link to={`/bot/${p.bot_b_id}`} className={`hover:text-brand-600 ${w === 1 ? 'font-semibold text-success-600' : w === 0 ? 'text-slate-400' : ''}`}>
                  {p.bot_b_display || p.bot_b_name || `#${p.bot_b_id}`}
                </Link>
              </span>
              <span className="text-xs text-slate-400">{p.status}</span>
              {p.match_id && (
                <Link to={`/watch/${p.match_id}`} className="ml-auto text-brand-600 hover:text-brand-700">
                  观战
                </Link>
              )}
            </li>
          )
        })}
      </ul>
    </PageStub>
  )
}
