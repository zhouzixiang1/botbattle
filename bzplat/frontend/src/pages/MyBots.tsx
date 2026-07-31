import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { useAuth } from '../components/useAuth'
import { apiForm, apiGet, apiJson, errMsg } from '../api'

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
}

export default function MyBots() {
  const { isLoggedIn } = useAuth()
  const [bots, setBots] = useState<Bot[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const [file, setFile] = useState<File | null>(null)

  const load = useCallback(async () => {
    if (!isLoggedIn) return
    try {
      const d = await apiGet<{ bots: Bot[] }>('/api/bots/mine')
      setBots(d.bots || [])
      setError('')
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    }
  }, [isLoggedIn])

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
      <p className="mb-4">上传二进制 Bot（Linux ELF / Windows PE）。macOS Mach-O 会被拒绝。</p>

      <form
        onSubmit={(e) => void onUpload(e)}
        className="mb-8 max-w-lg space-y-3 rounded-xl border border-slate-200 bg-white p-4"
      >
        <h2 className="text-sm font-medium text-slate-700">上传新 Bot</h2>
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

      <ul className="divide-y divide-slate-700/80 overflow-hidden rounded-xl border border-slate-200 bg-white">
        {bots.length === 0 ? (
          <li className="px-4 py-8 text-center text-slate-500">暂无 Bot，请先上传</li>
        ) : (
          bots.map((b) => (
            <li key={b.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
              <div className="min-w-0 flex-1">
                <div className="font-medium text-slate-800">
                  {b.display_name || b.name}
                  <span className="ml-2 font-mono text-xs text-slate-500">#{b.id}</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-2 text-xs text-slate-400">
                  <span className="rounded bg-slate-100/80 px-1.5 py-0.5">
                    {b.os || '—'} / {b.arch || '—'}
                  </span>
                  <span className="rounded bg-slate-100/80 px-1.5 py-0.5">
                    format: {b.format || 'unknown'}
                  </span>
                  <span>v{b.current_version ?? 0}</span>
                  <span>{b.is_active ? '启用' : '停用'}</span>
                </div>
              </div>
              <button
                type="button"
                onClick={() => void toggleActive(b)}
                className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-100"
              >
                {b.is_active ? '停用' : '启用'}
              </button>
            </li>
          ))
        )}
      </ul>
    </PageStub>
  )
}
