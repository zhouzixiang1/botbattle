import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Heart, Trash2, MessageSquare } from 'lucide-react'
import { apiGet, apiJson, apiPost, errMsg } from '@/api'
import { useAuth } from '@/components/useAuth'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { EmptyState, ErrorMsg } from '@/components/ui/status'
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
    <Card className="mt-4">
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2 text-base">
            <MessageSquare className="size-4 text-muted-foreground" />
            评论（{comments.length}）
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
      <CardContent className="space-y-3">
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
          <EmptyState text="暂无评论" className="py-6" icon={<MessageSquare className="size-7 opacity-40" />} />
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
      </CardContent>
    </Card>
  )
}
