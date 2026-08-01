import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft } from 'lucide-react'
import { apiGet, apiJson, errMsg } from '../../api'
import { ErrorMsg, Loading, RefreshBtn } from './ui'

// ── 类型 ──────────────────────────────────────────────────────
type GameId = 'holdem' | 'gomoku' | 'pencil'
type StageType =
  | 'round_robin'
  | 'double_round_robin'
  | 'group_round_robin'
  | 'group_double_round_robin'
  | 'swiss'
  | 'single_elimination'
type Scoring = 'poker_3_1_0' | 'ccgc_2_1_0'

interface Stage {
  key?: string
  type: StageType
  scoring?: Scoring
  rounds?: number
  group_count?: number
  advance_count?: number
  advance_per_group?: number
  rest_after_minutes?: number
  allow_bot_swap_in_rest?: boolean
}
interface Template {
  id: string
  name: string
  game_id: GameId
  match_config: { hands?: number; n_dots?: number }
  stages: Stage[]
  is_builtin?: number | boolean
}

const GAMES: { id: GameId; label: string }[] = [
  { id: 'holdem', label: '德州扑克' },
  { id: 'gomoku', label: '五子棋' },
  { id: 'pencil', label: '点格棋' },
]
const STAGE_TYPES: { id: StageType; label: string }[] = [
  { id: 'round_robin', label: '单循环' },
  { id: 'double_round_robin', label: '双循环' },
  { id: 'group_round_robin', label: '分组单循环' },
  { id: 'group_double_round_robin', label: '分组双循环' },
  { id: 'swiss', label: '瑞士轮' },
  { id: 'single_elimination', label: '单败淘汰' },
]
const GROUP_TYPES: StageType[] = ['group_round_robin', 'group_double_round_robin']
const SCORINGS: { id: Scoring; label: string }[] = [
  { id: 'poker_3_1_0', label: '3/1/0（扑克）' },
  { id: 'ccgc_2_1_0', label: '2/1/0（CCGC）' },
]

const emptyStage = (i: number): Stage => ({ key: `stage${i + 1}`, type: 'round_robin', scoring: 'poker_3_1_0' })
const emptyTemplate = (): Template => ({
  id: '',
  name: '',
  game_id: 'holdem',
  match_config: { hands: 70 },
  stages: [emptyStage(0)],
})

const inp = 'mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2'

export default function TemplatesTab() {
  const [list, setList] = useState<Template[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<Template | null>(null)
  const [isNew, setIsNew] = useState(false)
  const [busy, setBusy] = useState(false)
  const [ok, setOk] = useState('')
  const [previewN, setPreviewN] = useState(8)
  const [preview, setPreview] = useState<{ per_stage: number[]; total: number } | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const d = await apiGet<{ templates: Template[] }>('/api/admin/templates')
      setList(d.templates || [])
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // 切换 game 时重置 match_config 为该游戏默认
  const changeGame = (gid: GameId) => {
    if (!editing) return
    const mc =
      gid === 'holdem' ? { hands: 70 } : gid === 'pencil' ? { n_dots: 11 } : {}
    setEditing({ ...editing, game_id: gid, match_config: mc })
  }

  const patchStage = (i: number, patch: Partial<Stage>) => {
    if (!editing) return
    const stages = editing.stages.map((s, idx) => (idx === i ? { ...s, ...patch } : s))
    setEditing({ ...editing, stages })
  }

  const doPreview = async () => {
    if (!editing) return
    setError('')
    setOk('')
    try {
      const r = await apiJson<{ per_stage: number[]; total: number }>(
        '/api/admin/templates/preview',
        'POST',
        { stages: editing.stages, n: previewN },
      )
      setPreview(r)
    } catch (e) {
      setError(errMsg(e, '预览失败：配置可能有误'))
      setPreview(null)
    }
  }

  const save = async () => {
    if (!editing) return
    setBusy(true)
    setError('')
    setOk('')
    try {
      if (isNew) {
        await apiJson('/api/admin/templates', 'POST', editing)
      } else {
        await apiJson(`/api/admin/templates/${editing.id}`, 'PUT', editing)
      }
      setOk('已保存')
      setEditing(null)
      await load()
    } catch (e) {
      setError(errMsg(e, '保存失败'))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (id: string) => {
    if (!confirm(`删除模板 ${id}？`)) return
    try {
      await apiJson(`/api/admin/templates/${id}`, 'DELETE')
      await load()
    } catch (e) {
      setError(errMsg(e, '删除失败'))
    }
  }

  if (loading) return <Loading />
  if (editing) {
    return (
      <Editor
        editing={editing}
        isNew={isNew}
        busy={busy}
        error={error}
        ok={ok}
        preview={preview}
        previewN={previewN}
        setPreviewN={setPreviewN}
        changeGame={changeGame}
        patchStage={patchStage}
        setEditing={setEditing}
        addStage={() => setEditing({ ...editing, stages: [...editing.stages, emptyStage(editing.stages.length)] })}
        delStage={(i) => setEditing({ ...editing, stages: editing.stages.filter((_, idx) => idx !== i) })}
        setMatchConfig={(k, v) =>
          setEditing({ ...editing, match_config: { ...editing.match_config, [k]: v } })
        }
        setField={(k, v) => setEditing({ ...editing, [k]: v })}
        onPreview={doPreview}
        onSave={save}
      />
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-slate-400">赛制模板：阶段对阵 + 计分 + 对局参数。内置模板可改不可删。</p>
        <div className="flex gap-2">
          <RefreshBtn onClick={load} />
          <button
            type="button"
            onClick={() => {
              setIsNew(true)
              setEditing(emptyTemplate())
              setPreview(null)
            }}
            className="rounded-lg bg-brand-600 px-3 py-1.5 text-xs text-white hover:bg-brand-500"
          >
            + 新建模板
          </button>
        </div>
      </div>
      <ErrorMsg msg={error} />

      <ul className="divide-y divide-slate-100 overflow-hidden rounded-xl border border-slate-200 bg-white">
        {list.map((t) => (
          <li key={t.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
            <div>
              <span className="font-medium text-slate-700">{t.name}</span>
              {t.is_builtin ? (
                <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">内置</span>
              ) : null}
              <div className="font-mono text-xs text-slate-400">
                {t.id} · {t.game_id} · {t.stages?.length || 0} 阶段
              </div>
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => {
                  setIsNew(false)
                  setEditing({ ...t, match_config: t.match_config || {} })
                  setPreview(null)
                }}
                className="rounded-lg border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-50"
              >
                编辑
              </button>
              {!t.is_builtin && (
                <button
                  type="button"
                  onClick={() => remove(t.id)}
                  className="rounded-lg border border-error-300 px-3 py-1 text-xs text-error-600 hover:bg-error-50"
                >
                  删除
                </button>
              )}
            </div>
          </li>
        ))}
        {!list.length && <li className="px-4 py-8 text-center text-slate-400">无模板</li>}
      </ul>
    </div>
  )
}

// ── 编辑器（图形化分阶段表单） ──────────────────────────────────
function Editor(props: {
  editing: Template
  isNew: boolean
  busy: boolean
  error: string
  ok: string
  preview: { per_stage: number[]; total: number } | null
  previewN: number
  setPreviewN: (n: number) => void
  changeGame: (g: GameId) => void
  patchStage: (i: number, p: Partial<Stage>) => void
  setEditing: (t: Template | null) => void
  addStage: () => void
  delStage: (i: number) => void
  setMatchConfig: (k: string, v: number) => void
  setField: (k: 'id' | 'name', v: string) => void
  onPreview: () => void
  onSave: () => void
}) {
  const { editing: t, isNew, busy, error, ok, preview } = props
  return (
    <div className="space-y-4 rounded-xl border border-slate-200 bg-white p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-800">{isNew ? '新建模板' : '编辑模板'}</h3>
        <button
          type="button"
          onClick={() => props.setEditing(null)}
          className="text-xs text-slate-500 hover:text-slate-700"
        >
          <ArrowLeft className="inline size-3" /> 返回列表
        </button>
      </div>
      <ErrorMsg msg={error} />
      {ok && <p className="text-sm text-emerald-600">{ok}</p>}

      {/* 基本信息 */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <label className="text-sm text-slate-600">
          模板 id（小写字母/数字/_）
          <input className={inp} value={t.id} disabled={!isNew}
            onChange={(e) => props.setField('id', e.target.value)} />
        </label>
        <label className="text-sm text-slate-600">
          名称
          <input className={inp} value={t.name}
            onChange={(e) => props.setField('name', e.target.value)} />
        </label>
        <label className="text-sm text-slate-600">
          游戏
          <select className={inp} value={t.game_id} onChange={(e) => props.changeGame(e.target.value as GameId)}>
            {GAMES.map((g) => (
              <option key={g.id} value={g.id}>{g.label}</option>
            ))}
          </select>
        </label>
      </div>

      {/* 对局参数 match_config */}
      <div className="rounded-lg bg-slate-50 p-3">
        <p className="mb-2 text-xs font-medium text-slate-500">对局参数（match_config）</p>
        {t.game_id === 'holdem' && (
          <label className="text-sm text-slate-600">
            每场手数（1–500）
            <input type="number" min={1} max={500} className={inp} value={t.match_config.hands ?? 70}
              onChange={(e) => props.setMatchConfig('hands', Number(e.target.value))} />
          </label>
        )}
        {t.game_id === 'pencil' && (
          <label className="text-sm text-slate-600">
            点阵边长 n_dots（3–15）
            <input type="number" min={3} max={15} className={inp} value={t.match_config.n_dots ?? 11}
              onChange={(e) => props.setMatchConfig('n_dots', Number(e.target.value))} />
          </label>
        )}
        {t.game_id === 'gomoku' && <p className="text-xs text-slate-400">五子棋单局，无可调参数。</p>}
      </div>

      {/* 阶段列表 */}
      <div className="space-y-3">
        <p className="text-xs font-medium text-slate-500">阶段（按顺序执行）</p>
        {t.stages.map((s, i) => (
          <div key={i} className="rounded-lg border border-slate-200 p-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-medium text-slate-600">阶段 {i + 1}</span>
              <button type="button" onClick={() => props.delStage(i)}
                className="text-xs text-error-600 hover:underline">删除</button>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <label className="text-sm text-slate-600">
                标识 key
                <input className={inp} value={s.key ?? ''} onChange={(e) => props.patchStage(i, { key: e.target.value })} />
              </label>
              <label className="text-sm text-slate-600">
                类型
                <select className={inp} value={s.type} onChange={(e) => props.patchStage(i, { type: e.target.value as StageType })}>
                  {STAGE_TYPES.map((st) => <option key={st.id} value={st.id}>{st.label}</option>)}
                </select>
              </label>
              <label className="text-sm text-slate-600">
                计分
                <select className={inp} value={s.scoring ?? 'poker_3_1_0'} onChange={(e) => props.patchStage(i, { scoring: e.target.value as Scoring })}>
                  {SCORINGS.map((sc) => <option key={sc.id} value={sc.id}>{sc.label}</option>)}
                </select>
              </label>
              <label className="text-sm text-slate-600">
                晋级人数 advance_count
                <input type="number" min={1} className={inp} value={s.advance_count ?? ''}
                  onChange={(e) => props.patchStage(i, { advance_count: e.target.value === '' ? undefined : Number(e.target.value) })} />
              </label>
              {GROUP_TYPES.includes(s.type) && (
                <>
                  <label className="text-sm text-slate-600">
                    分组数 group_count
                    <input type="number" min={1} className={inp} value={s.group_count ?? 4}
                      onChange={(e) => props.patchStage(i, { group_count: Number(e.target.value) })} />
                  </label>
                  <label className="text-sm text-slate-600">
                    每组晋级 advance_per_group
                    <input type="number" min={1} className={inp} value={s.advance_per_group ?? ''}
                      onChange={(e) => props.patchStage(i, { advance_per_group: e.target.value === '' ? undefined : Number(e.target.value) })} />
                  </label>
                </>
              )}
              {s.type === 'swiss' && (
                <label className="text-sm text-slate-600">
                  轮数 rounds（0=自动）
                  <input type="number" min={0} className={inp} value={s.rounds ?? 0}
                    onChange={(e) => props.patchStage(i, { rounds: Number(e.target.value) })} />
                </label>
              )}
              <label className="text-sm text-slate-600">
                休息分钟 rest_after_minutes
                <input type="number" min={0} className={inp} value={s.rest_after_minutes ?? 0}
                  onChange={(e) => props.patchStage(i, { rest_after_minutes: Number(e.target.value) })} />
              </label>
              <label className="flex items-center gap-2 self-end pb-2 text-sm text-slate-600">
                <input type="checkbox" className="h-4 w-4" checked={!!s.allow_bot_swap_in_rest}
                  onChange={(e) => props.patchStage(i, { allow_bot_swap_in_rest: e.target.checked })} />
                休息期允许换 Bot
              </label>
            </div>
          </div>
        ))}
        <button type="button" onClick={props.addStage}
          className="rounded-lg border border-dashed border-slate-300 px-3 py-2 text-xs text-slate-500 hover:bg-slate-50">
          + 增加阶段
        </button>
      </div>

      {/* 预览 */}
      <div className="rounded-lg bg-slate-50 p-3">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-sm text-slate-600">
            预览人数 n
            <input type="number" min={2} className={`${inp} w-24`} value={props.previewN}
              onChange={(e) => props.setPreviewN(Number(e.target.value))} />
          </label>
          <button type="button" onClick={props.onPreview}
            className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs text-slate-600 hover:bg-slate-50">
            预估场数
          </button>
          {preview && (
            <span className="text-xs text-slate-500">
              各阶段：[{preview.per_stage.join(', ')}] · 合计 <strong className="text-slate-700">{preview.total}</strong> 场
            </span>
          )}
        </div>
      </div>

      <div className="flex gap-2">
        <button type="button" disabled={busy} onClick={props.onSave}
          className="rounded-lg bg-brand-600 px-4 py-2 text-sm text-white hover:bg-brand-500 disabled:opacity-50">
          保存
        </button>
        <button type="button" onClick={() => props.setEditing(null)}
          className="rounded-lg border border-slate-300 px-4 py-2 text-sm text-slate-600 hover:bg-slate-50">
          取消
        </button>
      </div>
    </div>
  )
}
