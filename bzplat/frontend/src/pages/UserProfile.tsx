import { useCallback, useEffect, useState } from 'react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import { UserPlus, UserCheck, Pencil, Bot as BotIcon, ArrowLeft } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar'
import { EmptyState, ErrorMsg } from '@/components/ui/status'
import { MetricCard } from '@/components/ui/metric-card'
import { useAuth } from '@/components/useAuth'
import Pagination from '@/components/Pagination'
import { apiGet, apiJson, errMsg } from '@/api'
import { toast } from 'sonner'
import { fmtDate } from '@/lib/format'
import { gameLabel, gameIcon } from '@/lib/games'

interface UserProfileData {
  id: number
  username: string
  display_name: string
  role: string
  bio: string
  avatar: string
  created_at: string
  last_login_at?: string
  xp?: number
  level?: number
  last_active_at?: string
  stats: {
    wins: number
    losses: number
    draws: number
    matches_played: number
    net_chips: number
    rated_bots: number
  }
  bot_count: number
}

interface BotRow {
  id: number
  name: string
  display_name: string
  game_id: string
  description?: string
  is_active: number | boolean
}

export default function UserProfile() {
  const { name } = useParams<{ name: string }>()
  const navigate = useNavigate()
  const { user } = useAuth()
  const isSelf = !!user && user.username === name

  const [profile, setProfile] = useState<UserProfileData | null>(null)
  const [bots, setBots] = useState<BotRow[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [following, setFollowing] = useState(false)
  const [followerCount, setFollowerCount] = useState(0)
  const [followingCount, setFollowingCount] = useState(0)
  // 分页
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 20

  const loadBots = useCallback(() => {
    if (!name) return Promise.resolve()
    const params = new URLSearchParams({ page: String(page), per_page: String(perPage) })
    return apiGet<{ bots: BotRow[]; total?: number }>(
      `/api/users/${encodeURIComponent(name)}/bots?${params.toString()}`,
    )
      .then((b) => {
        setBots(b.bots || [])
        if (b.total !== undefined) setTotal(b.total)
      })
      .catch((e) => setError(errMsg(e)))
  }, [name, page])

  useEffect(() => {
    if (!name) return
    setLoading(true)
    Promise.all([
      apiGet<{ profile: UserProfileData }>(`/api/users/${encodeURIComponent(name)}/profile`),
      loadBots(),
    ])
      .then(([p]) => {
        setProfile(p.profile)
        if (user && user.username !== name) {
          apiGet<{ following: boolean; follower_count: number; following_count: number }>(
            `/api/users/${p.profile.id}/follow-status`,
          )
            .then((fs) => {
              setFollowing(fs.following)
              setFollowerCount(fs.follower_count)
              setFollowingCount(fs.following_count)
            })
            .catch(() => {})
        }
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false))
  }, [name, user, loadBots])

  function toggleFollow() {
    if (!profile || !user) return
    const method = following ? 'DELETE' : 'POST'
    apiJson(`/api/users/${profile.id}/follow`, method)
      .then(() => {
        setFollowing(!following)
        setFollowerCount((c) => c + (following ? -1 : 1))
        toast.success(following ? '已取消关注' : '关注成功')
      })
      .catch((e) => setError(errMsg(e)))
  }

  if (loading) {
    return (
      <PageStub title="用户资料">
        {/* 骨架屏：头像+信息块 + 指标卡轮廓 */}
        <Card className="mb-4">
          <CardContent className="flex flex-col gap-5 py-5 sm:flex-row sm:items-center">
            <Skeleton className="size-20 rounded-full" />
            <div className="flex-1 space-y-2">
              <Skeleton className="h-6 w-32" />
              <Skeleton className="h-4 w-24" />
              <Skeleton className="h-4 w-40" />
            </div>
          </CardContent>
        </Card>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-20" />)}
        </div>
      </PageStub>
    )
  }
  if (error || !profile) {
    return (
      <PageStub title="用户资料">
        <ErrorMsg msg={error || '用户不存在或已停用'} className="mb-3" />
        {/* 后退策略：有历史则返回上一页，直接 URL 进入（无历史）fallback 到排行榜 */}
        <Button
          variant="ghost"
          size="sm"
          className="gap-1"
          onClick={() => (window.history.length > 1 ? navigate(-1) : navigate('/leaderboard'))}
        >
          <ArrowLeft className="size-4" /> 返回
        </Button>
      </PageStub>
    )
  }

  const displayName = profile.display_name || profile.username
  const avatarUrl = profile.avatar ? `/avatars/${profile.avatar}` : ''
  const totalGames = profile.stats.matches_played || 0
  const wins = profile.stats.wins || 0
  const wr = totalGames > 0 ? ((wins + (profile.stats.draws || 0) * 0.5) / totalGames) * 100 : 0

  return (
    <PageStub title={displayName}>
      {/* 顶部信息卡 */}
      <Card className="mb-5">
        <CardContent className="py-5">
          <div className="flex flex-wrap items-start gap-4">
            <Avatar className="size-20 border border-border">
              {avatarUrl && <AvatarImage src={avatarUrl} alt={displayName} />}
              <AvatarFallback className="bg-muted text-2xl font-bold text-muted-foreground">
                {displayName.charAt(0).toUpperCase()}
              </AvatarFallback>
            </Avatar>
            <div className="min-w-0 flex-1 space-y-1.5 overflow-hidden">
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <h2
                  className="max-w-full break-words text-xl font-bold text-foreground [overflow-wrap:anywhere]"
                  title={displayName}
                >
                  {displayName}
                </h2>
                {profile.role !== 'user' && (
                  <Badge variant={profile.role === 'admin' ? 'destructive' : 'secondary'}>
                    {profile.role === 'admin' ? '管理员' : '组织者'}
                  </Badge>
                )}
                {(profile.level ?? 0) > 0 && (
                  <Badge variant="outline" className="gap-1 border-warning/40 text-warning">
                    Lv.{profile.level}
                  </Badge>
                )}
              </div>
              <p className="text-sm text-muted-foreground">@{profile.username}</p>
              {profile.bio && (
                <p className="max-h-32 overflow-y-auto whitespace-pre-wrap break-words text-sm text-foreground/80 [overflow-wrap:anywhere]">
                  {profile.bio}
                </p>
              )}
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pt-1 text-xs text-muted-foreground">
                <span>注册于 {fmtDate(profile.created_at)}</span>
                {totalGames > 0 && <span>· 参与 {totalGames} 场对局</span>}
                {user && !isSelf && (
                  <>
                    <span>· 关注 {followingCount}</span>
                    <span>· 粉丝 {followerCount}</span>
                  </>
                )}
              </div>
              {(profile.xp ?? 0) > 0 && (
                <div className="max-w-xs pt-1">
                  <div className="flex items-center justify-between text-[10px] text-muted-foreground">
                    <span>经验 {profile.xp}</span>
                    <span>Lv.{profile.level ?? 0}</span>
                  </div>
                  <div className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full rounded-full bg-primary"
                      style={{ width: `${Math.min(100, ((profile.xp ?? 0) % 100) + (((profile.xp ?? 0) % 100) === 0 && (profile.xp ?? 0) > 0 ? 100 : 0))}%` }}
                    />
                  </div>
                </div>
              )}
            </div>

            {!isSelf && user && (
              <Button
                type="button"
                variant={following ? 'outline' : 'default'}
                size="sm"
                onClick={toggleFollow}
                className="gap-1.5"
              >
                {following ? <UserCheck className="size-3.5" /> : <UserPlus className="size-3.5" />}
                {following ? '已关注' : '关注'}
              </Button>
            )}
            {isSelf && (
              <Button asChild variant="outline" size="sm" className="gap-1.5">
                <Link to="/settings"><Pencil className="size-3.5" />编辑资料</Link>
              </Button>
            )}
          </div>

          {/* 总战绩 */}
          <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <MetricCard label="总胜率" value={`${wr.toFixed(1)}%`} />
            <MetricCard label="胜" value={wins} />
            <MetricCard label="负/平" value={profile.stats.losses || 0} hint={`平 ${profile.stats.draws || 0}`} danger />
            <MetricCard label="Bot 数" value={profile.bot_count} icon={<BotIcon className="size-5" />} />
          </div>
        </CardContent>
      </Card>

      {/* Bot 列表 */}
      <h3 className="mb-3 text-sm font-semibold text-foreground">Bot 列表（{total || bots.length}）</h3>
      {bots.length === 0 ? (
        <Card>
          <EmptyState text="暂无公开 Bot" icon={<BotIcon className="size-7 opacity-40" />} />
        </Card>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {bots.map((b) => {
            const GameIcon = gameIcon(b.game_id)
            return (
              <Link key={b.id} to={`/bot/${b.id}`} className="group">
                <Card className="h-full transition-colors hover:border-primary/40 hover:shadow-lift">
                  <CardContent className="gap-1 py-4">
                    <div className="flex items-center justify-between gap-2">
                      <span className="min-w-0 truncate font-medium text-foreground group-hover:text-primary" title={b.display_name || b.name}>
                        {b.display_name || b.name}
                      </span>
                      <Badge variant="secondary" className="gap-1 text-[10px]">
                        <GameIcon className="size-3" />
                        {gameLabel(b.game_id)}
                      </Badge>
                    </div>
                    <p className="truncate text-xs text-muted-foreground">@{b.name}</p>
                    {b.description && (
                      <p className="line-clamp-2 text-xs text-muted-foreground/80">{b.description}</p>
                    )}
                  </CardContent>
                </Card>
              </Link>
            )
          })}
        </div>
      )}
      <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
    </PageStub>
  )
}
