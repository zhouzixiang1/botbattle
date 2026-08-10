import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Heart, Trash2, MessageSquare } from 'lucide-react'
import { apiGet, apiJson, apiPost, errMsg } from '@/api'
import { useAuth } from '@/components/useAuth'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { EmptyState, ErrorMsg } from '@/components/ui/status'
import Pagination from '@/components/Pagination'
import { cn } from '@/lib/utils'
import { fmtTime } from '@/lib/format'

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
  // 分页（评论为密集列表，每页 20 条）
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 20

  const load = useCallback(() => {
    Promise.all([
      apiGet<{ comments: Comment[]; count: number; total?: number }>(
        `/api/comments?target_type=${targetType}&target_id=${encodeURIComponent(targetId)}&page=${page}&per_page=${perPage}`,
      ),
      user
        ? apiGet<{ liked: boolean; count: number }>(
            `/api/likes/status?target_type=${targetType}&target_id=${encodeURIComponent(targetId)}`,
          )
        : Promise.resolve(null),
    ])
      .then(([c, l]) => {
        setComments(c.comments || [])
        if (c.total !== undefined) setTotal(c.total)
        if (l) {
          setLiked(l.liked)
          setLikeCount(l.count)
        }
      })
      .catch((e) => setError(errMsg(e)))
  }, [targetType, targetId, user, page])

  useEffect(() => {
    load()
  }, [load])

  function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!body.trim()) return
    apiPost('/api/comments', 'POST', { target_type: targetType, target_id: targetId, body: body.trim() })
      .then(() => {
        setBody('')
        // 回第1页并强制刷新列表——仅 setPage(1) 在已是第1页时不触发 effect（审计 P1）
        if (page !== 1) {
          setPage(1)  // page 变化会触发 load
        } else {
          void load()  // 已在第1页，显式重载
        }
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
    <Card data-testid="comments-card" className="mt-4 gap-0 py-0">
      <CardHeader className="border-b px-4 py-3">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2 text-base">
            <MessageSquare className="size-4 text-muted-foreground" />
            评论（{total || comments.length}）
          </CardTitle>
          <Button
            type="button"
            variant={liked ? 'destructive' : 'outline'}
            size="sm"
            onClick={toggleLike}
            disabled={!user}
            className="gap-1.5"
          >
            <Heart className={cn('size-3.5', liked && 'fill-current')} />
            {likeCount}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 px-4 py-3">
        {error && <ErrorMsg msg={error} className="text-xs" />}
        {user && (
          <form onSubmit={submit} className="flex gap-2">
            <Input
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder="写下你的评论…"
              maxLength={2000}
              className="min-w-0 flex-1"
            />
            <Button type="submit" size="sm">发表</Button>
          </form>
        )}
        {comments.length === 0 ? (
          <EmptyState text="暂无评论" className="flex-row gap-1.5 py-2" icon={<MessageSquare className="size-4 opacity-40" />} />
        ) : (
          <div className="space-y-2">
            {comments.map((c) => (
              <div key={c.id} className="rounded-lg border border-border px-3 py-2 text-sm">
                <div className="flex items-center gap-2">
                  {c.username ? (
                    <Link
                      to={`/user/${encodeURIComponent(c.username)}`}
                      className="font-medium text-foreground hover:text-primary"
                    >
                      {c.user_display || c.username}
                    </Link>
                  ) : (
                    <span className="text-muted-foreground">已注销</span>
                  )}
                  <span className="text-xs text-muted-foreground">
                    {fmtTime(c.created_at)}
                  </span>
                  {user && (user.id === c.user_id || user.role === 'admin') && (
                    <button
                      type="button"
                      onClick={() => del(c.id)}
                      className="ml-auto inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-destructive"
                    >
                      <Trash2 className="size-3" />删除
                    </button>
                  )}
                </div>
                <p className="mt-1 whitespace-pre-wrap break-all text-foreground/80">{c.body}</p>
              </div>
            ))}
          </div>
        )}
        <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
      </CardContent>
    </Card>
  )
}
