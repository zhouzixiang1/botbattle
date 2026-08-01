import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { useAuth } from '../components/useAuth'
import { apiForm, apiGet, apiJson, errMsg } from '../api'
import { GAMES, gameLabel } from '../lib/games'

interface Bot {
  id: number
  name: string
  display_name?: string
  description?: string
  os?: string
  arch?: string
  format?: string
  current_version?: number
  is_public?: number
  is_active?: number
  updated_at?: string
  game_id?: string
}

export default function MyBots() {
  const { isLoggedIn } = useAuth()
  const [bots, setBots] = useState<Bot[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const [gameId, setGameId] = useState('holdem')
  const [filterGame, setFilterGame] = useState('')
  const [file, setFile] = useState<File | null>(null)

  const load = useCallback(async () => {
    if (!isLoggedIn) return
    try {
      const q = filterGame ? `?game_id=${encodeURIComponent(filterGame)}` : ''
      const d = await apiGet<{ bots: Bot[] }>(`/api/bots/mine${q}`)
      setBots(d.bots || [])
      setError('')
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    }
  }, [isLoggedIn, filterGame])

  useEffect(() => {
    void load()
  }, [load])

  const onUpload = async (e: FormEvent) => {
    e.preventDefault()
    if (!file) {
      setError('请选择二进制文件')
      return
    }
    setBusy(true)
    setError('')
    try {
      await apiForm('/api/bots', 'POST', {
        name,
        display_name: displayName,
        description,
        is_public: true,
        game_id: gameId,
        file,
      })
      setName('')
      setDisplayName('')
      setDescription('')
      setFile(null)
      await load()
    } catch (err) {
      setError(errMsg(err, '上传失败'))
    } finally {
      setBusy(false)
    }
  }

  const toggleActive = async (bot: Bot) => {
    try {
      await apiJson(
        `/api/bots/${bot.id}/active?active=${bot.is_active ? 'false' : 'true'}`,
        'POST',
      )
      await load()
    } catch (e) {
      setError(errMsg(e, '更新失败'))
    }
  }

  const [editing, setEditing] = useState<number | null>(null)
  const [editDisplay, setEditDisplay] = useState('')
  const [editDesc, setEditDesc] = useState('')

  const startEdit = (b: Bot) => {
    setEditing(b.id)
    setEditDisplay(b.display_name || '')
    setEditDesc(b.description || '')
  }

  const saveEdit = async (b: Bot) => {
    try {
      await apiJson(`/api/bots/${b.id}`, 'PATCH', {
        display_name: editDisplay, description: editDesc,
      })
      setEditing(null)
      await load()
    } catch (e) {
      setError(errMsg(e, '更新失败'))
    }
  }

  const togglePublic = async (b: Bot) => {
    try {
      await apiJson(`/api/bots/${b.id}`, 'PATCH', { is_public: !b.is_public })
      await load()
    } catch (e) {
      setError(errMsg(e, '更新失败'))
    }
  }

  const del = async (b: Bot) => {
    if (!confirm(`确定删除 ${b.display_name || b.name}？（将停用并设为私有）`)) return
    try {
      await apiJson(`/api/bots/${b.id}`, 'DELETE')
      await load()
    } catch (e) {
      setError(errMsg(e, '删除失败'))
    }
  }

  if (!isLoggedIn) {
    return (
      <PageStub title="我的 Bot">
        <p>
          请先{' '}
          <Link to="/login" className="text-brand-600 hover:text-brand-700">
            登录
          </Link>{' '}
          后管理 Bot。
        </p>
      </PageStub>
    )
  }

  return (
    <PageStub title="我的 Bot">
      <p className="mb-4">
        上传二进制 Bot（Linux ELF / Windows PE）。请选择对应游戏类型。macOS Mach-O 会被拒绝。
      </p>

      <form
        onSubmit={(e) => void onUpload(e)}
        className="mb-8 max-w-lg space-y-3 rounded-xl border border-slate-200 bg-white p-4"
      >
        <h2 className="text-sm font-medium text-slate-700">上传新 Bot</h2>
        <label className="block text-sm text-slate-600">
          游戏类型
          <select
            value={gameId}
            onChange={(e) => setGameId(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-slate-800 focus:border-brand-400 focus:outline-none"
          >
            {GAMES.map((g) => (
              <option key={g.id} value={g.id}>
                {g.label}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-sm text-slate-600">
          名称（唯一标识）
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            pattern="[A-Za-z0-9_\-]+"
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-slate-800 focus:border-brand-400 focus:outline-none"
          />
        </label>
        <label className="block text-sm text-slate-600">
          显示名
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-slate-800 focus:border-brand-400 focus:outline-none"
          />
        </label>
        <label className="block text-sm text-slate-600">
          简介
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-slate-800 focus:border-brand-400 focus:outline-none"
          />
        </label>
        <label className="block text-sm text-slate-600">
          二进制文件
          <input
            type="file"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            required
            className="mt-1 block w-full text-sm text-slate-400 file:mr-3 file:rounded-lg file:border-0 file:bg-brand-700 file:px-3 file:py-1.5 file:text-sm file:text-white"
          />
        </label>
        {error && <p className="text-sm text-error-500">{error}</p>}
        <button
          type="submit"
          disabled={busy}
          className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-500 disabled:opacity-60"
        >
          {busy ? '上传中…' : '上传'}
        </button>
      </form>

      <div className="mb-3">
        <label className="text-sm text-slate-500">
          筛选游戏
          <select
            value={filterGame}
            onChange={(e) => setFilterGame(e.target.value)}
            className="ml-2 rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-slate-700"
          >
            <option value="">全部</option>
            {GAMES.map((g) => (
              <option key={g.id} value={g.id}>
                {g.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <ul className="divide-y divide-slate-700/80 overflow-hidden rounded-xl border border-slate-200 bg-white">
        {bots.length === 0 ? (
          <li className="px-4 py-8 text-center text-slate-500">暂无 Bot，请先上传</li>
        ) : (
          bots.map((b) => (
            <li key={b.id} className="px-4 py-3">
              <div className="flex flex-wrap items-center gap-3">
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-slate-800">
                    <Link to={`/bot/${b.id}`} className="hover:text-brand-600">
                      {b.display_name || b.name}
                    </Link>
                    <span className="ml-2 font-mono text-xs text-slate-500">#{b.id}</span>
                    <span className="ml-2 rounded bg-brand-50 px-1.5 py-0.5 text-xs text-brand-700">
                      {gameLabel(b.game_id)}
                    </span>
                  </div>
                  {b.description && (
                    <p className="mt-0.5 text-xs text-slate-500">{b.description}</p>
                  )}
                  <div className="mt-1 flex flex-wrap gap-2 text-xs text-slate-400">
                    <span className="rounded bg-slate-100/80 px-1.5 py-0.5">
                      {b.os || '—'} / {b.arch || '—'}
                    </span>
                    <span className="rounded bg-slate-100/80 px-1.5 py-0.5">
                      format: {b.format || 'unknown'}
                    </span>
                    <span>v{b.current_version ?? 0}</span>
                    <span>{b.is_active ? '启用' : '停用'}</span>
                    <span>{b.is_public ? '公开' : '私有'}</span>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  <button type="button" onClick={() => void toggleActive(b)}
                    className="rounded-lg border border-slate-300 px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-100">
                    {b.is_active ? '停用' : '启用'}
                  </button>
                  <button type="button" onClick={() => void togglePublic(b)}
                    className="rounded-lg border border-slate-300 px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-100">
                    {b.is_public ? '设私有' : '设公开'}
                  </button>
                  <button type="button" onClick={() => startEdit(b)}
                    className="rounded-lg border border-slate-300 px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-100">
                    编辑
                  </button>
                  <button type="button" onClick={() => void del(b)}
                    className="rounded-lg border border-error-200 px-2.5 py-1 text-xs text-error-600 hover:bg-error-50">
                    删除
                  </button>
                </div>
              </div>
              {editing === b.id && (
                <div className="mt-2 flex flex-wrap items-end gap-2 rounded-lg bg-slate-50 p-3">
                  <label className="text-xs text-slate-500">
                    显示名
                    <input value={editDisplay} onChange={(e) => setEditDisplay(e.target.value)} maxLength={64}
                      className="mt-1 block rounded border border-slate-300 bg-white px-2 py-1 text-sm" />
                  </label>
                  <label className="text-xs text-slate-500">
                    简介
                    <input value={editDesc} onChange={(e) => setEditDesc(e.target.value)} maxLength={500}
                      className="mt-1 block w-64 rounded border border-slate-300 bg-white px-2 py-1 text-sm" />
                  </label>
                  <button type="button" onClick={() => void saveEdit(b)}
                    className="rounded-lg bg-brand-600 px-3 py-1.5 text-xs text-white hover:bg-brand-500">保存</button>
                  <button type="button" onClick={() => setEditing(null)}
                    className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-100">取消</button>
                </div>
              )}
            </li>
          ))
        )}
      </ul>
    </PageStub>
  )
}
