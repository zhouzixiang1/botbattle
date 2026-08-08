import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft } from 'lucide-react'
import { apiGet, apiJson, errMsg } from '../../api'
import { ErrorMsg, Loading, RefreshBtn, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Switch, inp } from './ui'
import { useConfirm } from '@/hooks/use-confirm'
import { GAMES, type GameId } from '@/games'

// ── 类型 ──────────────────────────────────────────────────────
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
  match_config: Record<string, number>
  stages: Stage[]
  is_builtin?: number | boolean
}

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
  match_config: {},
  stages: [emptyStage(0)],
})

export default function TemplatesTab() {
  const [confirm, confirmDialog] = useConfirm()
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

  // 切换 game：规则参数已钉死，match_config 恒空
  const changeGame = (gid: GameId) => {
    if (!editing) return
    setEditing({ ...editing, game_id: gid, match_config: {} })
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
        { stages: editing.stages, n: previewN, game_id: editing.game_id },
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
    if (!await confirm({
      title: '删除模板',
      desc: `删除模板 ${id}？`,
      confirmText: '删除',
      danger: true,
    })) return
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
        setField={(k, v) => setEditing({ ...editing, [k]: v })}
        onPreview={doPreview}
        onSave={save}
      />
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">赛制模板：阶段对阵 + 计分 + 对局参数。内置模板可改不可删。</p>
        <div className="flex gap-2">
          <RefreshBtn onClick={load} />
          <button
            type="button"
            onClick={() => {
              setIsNew(true)
              setEditing(emptyTemplate())
              setPreview(null)
            }}
            className="rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground hover:bg-primary/90"
          >
            + 新建模板
          </button>
        </div>
      </div>
      <ErrorMsg msg={error} />

      <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-card">
        {list.map((t) => (
          <li key={t.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
            <div>
              <span className="font-medium text-foreground">{t.name}</span>
              {t.is_builtin ? (
                <span className="ml-2 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">内置</span>
              ) : null}
              <div className="font-mono text-xs text-muted-foreground">
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
                className="rounded-lg border border-input bg-card px-3 py-1 text-xs text-muted-foreground hover:bg-accent"
              >
                编辑
              </button>
              {!t.is_builtin && (
                <button
                  type="button"
                  onClick={() => remove(t.id)}
                  className="rounded-lg border border-destructive/30 bg-card px-3 py-1 text-xs text-destructive hover:bg-destructive/10"
                >
                  删除
                </button>
              )}
            </div>
          </li>
        ))}
        {!list.length && <li className="px-4 py-8 text-center text-muted-foreground">无模板</li>}
      </ul>
      {confirmDialog}
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
  setField: (k: 'id' | 'name', v: string) => void
  onPreview: () => void
  onSave: () => void
}) {
  const { editing: t, isNew, busy, error, ok, preview } = props
  return (
    <div className="space-y-4 rounded-xl border border-border bg-card p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-foreground">{isNew ? '新建模板' : '编辑模板'}</h3>
        <button
          type="button"
          onClick={() => props.setEditing(null)}
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="inline size-3" /> 返回列表
        </button>
      </div>
      <ErrorMsg msg={error} />
      {ok && <p className="text-sm text-success">{ok}</p>}

      {/* 基本信息 */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <label className="text-sm text-muted-foreground">
          模板 id（小写字母/数字/_）
          <input className={inp} value={t.id} disabled={!isNew}
            onChange={(e) => props.setField('id', e.target.value)} />
        </label>
        <label className="text-sm text-muted-foreground">
          名称
          <input className={inp} value={t.name}
            onChange={(e) => props.setField('name', e.target.value)} />
        </label>
        <div className="space-y-1 text-sm text-muted-foreground">
          游戏
          <Select value={t.game_id} onValueChange={(v) => props.changeGame(v as GameId)}>
            <SelectTrigger className="mt-1 h-9 w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {GAMES.map((g) => (
                <SelectItem key={g.id} value={g.id}>{g.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* 阶段列表 */}
      <div className="space-y-3">
        <p className="text-xs font-medium text-muted-foreground">阶段（按顺序执行）</p>
        {t.stages.map((s, i) => (
          <div key={i} className="rounded-lg border border-border p-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-medium text-muted-foreground">阶段 {i + 1}</span>
              <button type="button" onClick={() => props.delStage(i)}
                className="text-xs text-destructive hover:underline">删除</button>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <label className="text-sm text-muted-foreground">
                标识 key
                <input className={inp} value={s.key ?? ''} onChange={(e) => props.patchStage(i, { key: e.target.value })} />
              </label>
              <div className="space-y-1 text-sm text-muted-foreground">
                类型
                <Select value={s.type} onValueChange={(v) => props.patchStage(i, { type: v as StageType })}>
                  <SelectTrigger className="mt-1 h-9 w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {STAGE_TYPES.map((st) => <SelectItem key={st.id} value={st.id}>{st.label}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1 text-sm text-muted-foreground">
                计分
                <Select value={s.scoring ?? 'poker_3_1_0'} onValueChange={(v) => props.patchStage(i, { scoring: v as Scoring })}>
                  <SelectTrigger className="mt-1 h-9 w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {SCORINGS.map((sc) => <SelectItem key={sc.id} value={sc.id}>{sc.label}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <label className="text-sm text-muted-foreground">
                晋级人数 advance_count
                <input type="number" min={1} className={inp} value={s.advance_count ?? ''}
                  onChange={(e) => props.patchStage(i, { advance_count: e.target.value === '' ? undefined : Number(e.target.value) })} />
              </label>
              {GROUP_TYPES.includes(s.type) && (
                <>
                  <label className="text-sm text-muted-foreground">
                    分组数 group_count
                    <input type="number" min={1} className={inp} value={s.group_count ?? 4}
                      onChange={(e) => props.patchStage(i, { group_count: Number(e.target.value) })} />
                  </label>
                  <label className="text-sm text-muted-foreground">
                    每组晋级 advance_per_group
                    <input type="number" min={1} className={inp} value={s.advance_per_group ?? ''}
                      onChange={(e) => props.patchStage(i, { advance_per_group: e.target.value === '' ? undefined : Number(e.target.value) })} />
                  </label>
                </>
              )}
              {s.type === 'swiss' && (
                <label className="text-sm text-muted-foreground">
                  轮数 rounds（0=自动）
                  <input type="number" min={0} className={inp} value={s.rounds ?? 0}
                    onChange={(e) => props.patchStage(i, { rounds: Number(e.target.value) })} />
                </label>
              )}
              <label className="text-sm text-muted-foreground">
                休息分钟 rest_after_minutes
                <input type="number" min={0} className={inp} value={s.rest_after_minutes ?? 0}
                  onChange={(e) => props.patchStage(i, { rest_after_minutes: Number(e.target.value) })} />
              </label>
              <div className="flex items-center gap-2 self-end pb-2 text-sm text-muted-foreground">
                <Switch checked={!!s.allow_bot_swap_in_rest}
                  onCheckedChange={(v) => props.patchStage(i, { allow_bot_swap_in_rest: v })} />
                休息期允许换 Bot
              </div>
            </div>
          </div>
        ))}
        <button type="button" onClick={props.addStage}
          className="rounded-lg border border-dashed border-input bg-card px-3 py-2 text-xs text-muted-foreground hover:bg-accent">
          + 增加阶段
        </button>
      </div>

      {/* 预览 */}
      <div className="rounded-lg bg-muted p-3">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-sm text-muted-foreground">
            预览人数 n
            <input type="number" min={2} className={`${inp} w-24`} value={props.previewN}
              onChange={(e) => props.setPreviewN(Number(e.target.value))} />
          </label>
          <button type="button" onClick={props.onPreview}
            className="rounded-lg border border-input bg-card px-3 py-2 text-xs text-muted-foreground hover:bg-accent">
            预估场数
          </button>
          {preview && (
            <span className="text-xs text-muted-foreground">
              各阶段：[{preview.per_stage.join(', ')}] · 合计 <strong className="text-foreground">{preview.total}</strong> 场
            </span>
          )}
        </div>
      </div>

      <div className="flex gap-2">
        <button type="button" disabled={busy} onClick={props.onSave}
          className="rounded-lg bg-primary px-4 py-2 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
          保存
        </button>
        <button type="button" onClick={() => props.setEditing(null)}
          className="rounded-lg border border-input bg-card px-4 py-2 text-sm text-muted-foreground hover:bg-accent">
          取消
        </button>
      </div>
    </div>
  )
}
