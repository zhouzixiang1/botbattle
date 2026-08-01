import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, apiJson, apiPost, errMsg } from '../api'
import { useAuth } from './useAuth'

interface Comment {
  id: number
  user_id: number
  username?: string
  user_display?: string
  body: string
  created_at: string
}

/** 评论区组件（target_type = match|bot）+ 可选点赞按钮 */
export default function Comments({
  targetType,
  targetId,
}: {
  targetType: 'match' | 'bot'
  targetId: string
}) {
  const { user } = useAuth()
  const [comments, setComments] = useState<Comment[]>([])
  const [body, setBody] = useState('')
  const [error, setError] = useState('')
  const [liked, setLiked] = useState(false)
  const [likeCount, setLikeCount] = useState(0)

  function load() {
    Promise.all([
      apiGet<{ comments: Comment[]; count: number }>(
        `/api/comments?target_type=${targetType}&target_id=${encodeURIComponent(targetId)}`,
      ),
      user
        ? apiGet<{ liked: boolean; count: number }>(
            `/api/likes/status?target_type=${targetType}&target_id=${encodeURIComponent(targetId)}`,
          )
        : Promise.resolve(null),
    ])
      .then(([c, l]) => {
        setComments(c.comments || [])
        if (l) {
          setLiked(l.liked)
          setLikeCount(l.count)
        }
      })
      .catch((e) => setError(errMsg(e)))
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targetId, user])

  function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!body.trim()) return
    apiPost('/api/comments', 'POST', { target_type: targetType, target_id: targetId, body: body.trim() })
      .then(() => {
        setBody('')
        load()
      })
      .catch((e) => setError(errMsg(e)))
  }

  function toggleLike() {
    if (!user) return
    const payload = { target_type: targetType, target_id: targetId }
    const m = liked ? 'DELETE' : 'POST'
    apiJson('/api/likes', m, payload)
      .then(() => {
        setLiked(!liked)
        setLikeCount((c) => Math.max(0, c + (liked ? -1 : 1)))
      })
      .catch((e) => setError(errMsg(e)))
  }

  function del(id: number) {
    apiJson(`/api/comments/${id}`, 'DELETE').then(() => load()).catch((e) => setError(errMsg(e)))
  }

  return (
    <div className="card mt-4 p-4">
      <div className="mb-3 flex items-center gap-3">
        <h3 className="text-sm font-semibold text-slate-700">
          评论（{comments.length}）
        </h3>
        <button
          type="button"
          onClick={toggleLike}
          disabled={!user}
          className={`rounded-lg px-3 py-1 text-xs font-medium ${
            liked
              ? 'bg-error-50 text-error-600'
              : 'border border-slate-300 bg-white text-slate-600 hover:bg-slate-50'
          } ${!user ? 'opacity-50' : ''}`}
        >
          {liked ? '♥' : '♡'} {likeCount}
        </button>
      </div>
      {error && <p className="mb-2 text-xs text-error-500">{error}</p>}
      {user && (
        <form onSubmit={submit} className="mb-3 flex gap-2">
          <input
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder="写下你的评论…"
            maxLength={2000}
            className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700"
          />
          <button
            type="submit"
            className="rounded-lg bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-500"
          >
            发表
          </button>
        </form>
      )}
      <div className="space-y-2">
        {comments.length === 0 ? (
          <p className="py-3 text-center text-xs text-slate-400">暂无评论</p>
        ) : (
          comments.map((c) => (
            <div key={c.id} className="rounded-lg border border-slate-100 px-3 py-2 text-sm">
              <div className="flex items-center gap-2">
                {c.username ? (
                  <Link
                    to={`/user/${encodeURIComponent(c.username)}`}
                    className="font-medium text-slate-700 hover:text-brand-600"
                  >
                    {c.user_display || c.username}
                  </Link>
                ) : (
                  <span className="text-slate-400">已注销</span>
                )}
                <span className="text-xs text-slate-400">
                  {c.created_at?.replace('T', ' ').slice(0, 16)}
                </span>
                {user && (user.id === c.user_id || user.role === 'admin') && (
                  <button
                    type="button"
                    onClick={() => del(c.id)}
                    className="ml-auto text-xs text-slate-400 hover:text-error-500"
                  >
                    删除
                  </button>
                )}
              </div>
              <p className="mt-1 whitespace-pre-wrap text-slate-600">{c.body}</p>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
