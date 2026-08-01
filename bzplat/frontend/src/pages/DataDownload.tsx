import { useEffect, useState } from 'react'
import PageStub from '../components/PageStub'
import { useAuth } from '../components/useAuth'
import { apiGet, errMsg } from '../api'
import { GAMES, gameLabel } from '../lib/games'

interface Pack {
  game_id: string
  month: string
  cnt: number
}

export default function DataDownload() {
  const { user } = useAuth()
  const [packs, setPacks] = useState<Pack[]>([])
  const [error, setError] = useState('')
  const [filterGame, setFilterGame] = useState('')

  useEffect(() => {
    const q = filterGame ? `?game_id=${encodeURIComponent(filterGame)}` : ''
    apiGet<{ packs: Pack[] }>(`/api/matchpacks${q}`)
      .then((d) => setPacks(d.packs || []))
      .catch((e) => setError(errMsg(e)))
  }, [filterGame])

  const level = user?.level ?? 0
  const gated = level < 1

  return (
    <PageStub title="对局数据集">
      <p className="mb-4 text-sm text-slate-500">
        下载已完成对局的数据集（按游戏 × 月份打包，gzip 压缩，每行一条 JSON 对局）。
        可用于 AI 训练、对局复盘与数据分析。
      </p>
      {gated && (
        <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700">
          ⚠ 下载数据集需 <strong>等级 ≥ 1</strong>（当前 Lv.{level}）。多参与对局/赛事/评论即可升级。
        </div>
      )}
      {error && <p className="mb-3 text-sm text-error-500">{error}</p>}
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
              <option key={g.id} value={g.id}>{g.label}</option>
            ))}
          </select>
        </label>
      </div>
      {packs.length === 0 ? (
        <p className="py-8 text-center text-sm text-slate-400">暂无可下载的数据集</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-200">
          <table className="w-full min-w-[28rem] text-left text-sm">
            <thead className="bg-white text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-3 py-2.5">游戏</th>
                <th className="px-3 py-2.5">月份</th>
                <th className="px-3 py-2.5">对局数</th>
                <th className="px-3 py-2.5">下载</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {packs.map((p) => (
                <tr key={`${p.game_id}-${p.month}`} className="bg-white hover:bg-slate-100/60">
                  <td className="px-3 py-2.5">{gameLabel(p.game_id)}</td>
                  <td className="px-3 py-2.5 font-mono text-slate-600">{p.month}</td>
                  <td className="px-3 py-2.5 text-slate-500">{p.cnt}</td>
                  <td className="px-3 py-2.5">
                    {gated ? (
                      <span className="text-xs text-slate-400">🔒 需 Lv.1</span>
                    ) : (
                      <a
                        href={`/api/matchpacks/download?game_id=${p.game_id}&month=${p.month}`}
                        className="text-brand-600 hover:text-brand-700"
                      >
                        下载 .gz ↓
                      </a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </PageStub>
  )
}
