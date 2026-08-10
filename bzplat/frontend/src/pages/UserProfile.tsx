import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Bot as BotIcon, Pencil, UserCheck, UserPlus, Users } from 'lucide-react'
import { toast } from 'sonner'

import { apiGet, apiJson, errMsg } from '@/api'
import { DataRegion, PageFrame, PageHeader, SummaryStrip } from '@/components/layout'
import Pagination from '@/components/Pagination'
import { useAuth } from '@/components/useAuth'
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { EntityName, OverflowText } from '@/components/ui/overflow-text'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { fmtDate } from '@/lib/format'
import { gameIcon, gameLabel } from '@/lib/games'
import { SummaryMetric } from '@/pages/public-page-ui'

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

const PER_PAGE = 20

export default function UserProfile() {
  const { name } = useParams<{ name: string }>()
  const navigate = useNavigate()
  const { user } = useAuth()
  const isSelf = Boolean(user && user.username === name)

  const [profile, setProfile] = useState<UserProfileData | null>(null)
  const [bots, setBots] = useState<BotRow[]>([])
  const [profileError, setProfileError] = useState('')
  const [botsError, setBotsError] = useState('')
  const [actionError, setActionError] = useState('')
  const [loading, setLoading] = useState(true)
  const [botsLoading, setBotsLoading] = useState(true)
  const [following, setFollowing] = useState(false)
  const [followerCount, setFollowerCount] = useState(0)
  const [followingCount, setFollowingCount] = useState(0)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)

  const loadBots = useCallback(() => {
    if (!name) return Promise.resolve()
    setBotsLoading(true)
    setBotsError('')
    const params = new URLSearchParams({ page: String(page), per_page: String(PER_PAGE) })
    return apiGet<{ bots: BotRow[]; total?: number }>(
      `/api/users/${encodeURIComponent(name)}/bots?${params.toString()}`,
    )
      .then((data) => {
        setBots(data.bots || [])
        setTotal(data.total ?? 0)
      })
      .catch((cause) => setBotsError(errMsg(cause)))
      .finally(() => setBotsLoading(false))
  }, [name, page])

  useEffect(() => {
    if (!name) return
    setLoading(true)
    setProfileError('')
    apiGet<{ profile: UserProfileData }>(`/api/users/${encodeURIComponent(name)}/profile`)
      .then(({ profile: nextProfile }) => {
        setProfile(nextProfile)
        if (user && user.username !== name) {
          apiGet<{ following: boolean; follower_count: number; following_count: number }>(
            `/api/users/${nextProfile.id}/follow-status`,
          )
            .then((status) => {
              setFollowing(status.following)
              setFollowerCount(status.follower_count)
              setFollowingCount(status.following_count)
            })
            .catch(() => {})
        }
      })
      .catch((cause) => setProfileError(errMsg(cause)))
      .finally(() => setLoading(false))
  }, [name, user])

  useEffect(() => {
    void loadBots()
  }, [loadBots])

  const toggleFollow = () => {
    if (!profile || !user) return
    const wasFollowing = following
    apiJson(`/api/users/${profile.id}/follow`, wasFollowing ? 'DELETE' : 'POST')
      .then(() => {
        setActionError('')
        setFollowing(!wasFollowing)
        setFollowerCount((count) => count + (wasFollowing ? -1 : 1))
        toast.success(wasFollowing ? '已取消关注' : '关注成功')
      })
      .catch((cause) => setActionError(errMsg(cause)))
  }

  if (loading) {
    return (
      <PageFrame width="default" layout="public-user-profile-loading">
        <PageHeader title="用户资料" description="正在读取用户资料与公开 Bot。" />
        <DataRegion title="用户概览" contentClassName="p-4">
          <div className="flex min-w-0 items-center gap-4">
            <Skeleton className="size-16 shrink-0 rounded-full" />
            <div className="min-w-0 flex-1 space-y-2"><Skeleton className="h-5 w-40 max-w-full" /><Skeleton className="h-4 w-28 max-w-full" /><Skeleton className="h-4 w-64 max-w-full" /></div>
          </div>
        </DataRegion>
        <SummaryStrip columns={4}>{[0, 1, 2, 3].map((item) => <Skeleton key={item} className="h-14" />)}</SummaryStrip>
      </PageFrame>
    )
  }

  if (profileError || !profile) {
    return (
      <PageFrame width="narrow" layout="public-user-profile-error">
        <PageHeader title="用户资料" description="无法显示该用户的公开资料。" />
        <DataRegion title="加载失败" contentClassName="space-y-3 px-4 py-5">
          <ErrorMsg msg={profileError || '用户不存在或已停用'} />
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => (window.history.length > 1 ? navigate(-1) : navigate('/leaderboard'))}
          >
            <ArrowLeft className="size-4" />返回
          </Button>
        </DataRegion>
      </PageFrame>
    )
  }

  const displayName = profile.display_name || profile.username
  const avatarUrl = profile.avatar ? `/avatars/${profile.avatar}` : ''
  const totalGames = profile.stats.matches_played || 0
  const wins = profile.stats.wins || 0
  const winRate = totalGames > 0 ? ((wins + (profile.stats.draws || 0) * 0.5) / totalGames) * 100 : 0
  const xpProgress = Math.min(100, ((profile.xp ?? 0) % 100) || ((profile.xp ?? 0) > 0 ? 100 : 0))

  return (
    <PageFrame width="default" layout="public-user-profile">
      <PageHeader
        eyebrow={`@${profile.username}`}
        title={<EntityName lines={2} tooltip={displayName} className="text-2xl font-bold sm:text-[1.75rem]">{displayName}</EntityName>}
        description={<OverflowText lines={2} className="whitespace-pre-wrap">{profile.bio || '该用户暂未填写个人简介。'}</OverflowText>}
        actions={
          isSelf ? (
            <Button asChild variant="outline" size="sm"><Link to="/settings"><Pencil className="size-4" />编辑资料</Link></Button>
          ) : user ? (
            <Button type="button" variant={following ? 'outline' : 'default'} size="sm" onClick={toggleFollow}>
              {following ? <UserCheck className="size-4" /> : <UserPlus className="size-4" />}
              {following ? '已关注' : '关注'}
            </Button>
          ) : undefined
        }
      />

      {actionError && <ErrorMsg msg={actionError} />}

      <DataRegion title="用户概览" description={`注册于 ${fmtDate(profile.created_at)}`} contentClassName="p-4">
        <div className="grid min-w-0 gap-4 sm:grid-cols-[4rem_minmax(0,1fr)] sm:items-start">
          <Avatar className="size-16 border">
            {avatarUrl && <AvatarImage src={avatarUrl} alt={`${displayName} 的头像`} />}
            <AvatarFallback className="bg-muted text-xl font-bold text-muted-foreground">{displayName.charAt(0).toUpperCase()}</AvatarFallback>
          </Avatar>
          <div className="min-w-0 space-y-2">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              {profile.role !== 'user' && <Badge variant={profile.role === 'admin' ? 'destructive' : 'secondary'}>{profile.role === 'admin' ? '管理员' : '组织者'}</Badge>}
              {(profile.level ?? 0) > 0 && <Badge variant="outline">Lv.{profile.level}</Badge>}
              {user && !isSelf && <><Badge variant="secondary">关注 {followingCount}</Badge><Badge variant="secondary">粉丝 {followerCount}</Badge></>}
            </div>
            {(profile.xp ?? 0) > 0 && (
              <div className="max-w-sm">
                <div className="flex min-w-0 items-center justify-between text-xs text-muted-foreground"><span>经验 {profile.xp}</span><span>Lv.{profile.level ?? 0}</span></div>
                <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted" role="progressbar" aria-label="当前等级经验进度" aria-valuenow={xpProgress} aria-valuemin={0} aria-valuemax={100}>
                  <div className="h-full rounded-full bg-primary" style={{ width: `${xpProgress}%` }} />
                </div>
              </div>
            )}
          </div>
        </div>
      </DataRegion>

      <SummaryStrip columns={4}>
        <SummaryMetric label="总胜率" value={`${winRate.toFixed(1)}%`} detail={`${totalGames} 场计分对局`} />
        <SummaryMetric label="胜" value={wins} detail={`负 ${profile.stats.losses || 0} · 平 ${profile.stats.draws || 0}`} />
        <SummaryMetric label="公开 Bot" value={profile.bot_count} detail={`${profile.stats.rated_bots || 0} 个已定级`} icon={<BotIcon className="size-4" />} />
        <SummaryMetric label="社交" value={user && !isSelf ? followerCount : '—'} detail={user && !isSelf ? '粉丝数' : '登录后可关注'} icon={<Users className="size-4" />} />
      </SummaryStrip>

      <DataRegion title="公开 Bot" description={`共 ${total || profile.bot_count} 个；当前第 ${page} 页`}>
        {botsError ? (
          <ErrorMsg msg={botsError} className="px-4 py-5" />
        ) : botsLoading ? (
          <Loading text="正在加载 Bot…" />
        ) : bots.length === 0 ? (
          <EmptyState text="暂无公开 Bot" icon={<BotIcon className="size-5 opacity-50" />} className="py-8" />
        ) : (
          <ul className="grid min-w-0 gap-2 p-3 sm:grid-cols-2 lg:grid-cols-3">
            {bots.map((bot) => {
              const GameIcon = gameIcon(bot.game_id)
              return (
                <li key={bot.id} className="min-w-0">
                  <Link to={`/bot/${bot.id}`} className="group min-w-0">
                    <Card density="compact" className="h-full transition-colors hover:border-primary/40 hover:bg-accent/30">
                      <CardContent className="min-w-0 space-y-1">
                        <div className="flex min-w-0 items-start gap-2">
                          <EntityName lines={2} tooltip={false} tooltipFocusable={false} className="min-w-0 flex-1 text-sm group-hover:text-primary">{bot.display_name || bot.name}</EntityName>
                          <Badge variant="secondary" className="shrink-0"><GameIcon className="size-3" />{gameLabel(bot.game_id)}</Badge>
                        </div>
                        <OverflowText tooltip={false} className="text-xs text-muted-foreground">@{bot.name}</OverflowText>
                        {bot.description && <OverflowText lines={2} tooltip={false} className="text-xs text-muted-foreground">{bot.description}</OverflowText>}
                      </CardContent>
                    </Card>
                  </Link>
                </li>
              )
            })}
          </ul>
        )}
      </DataRegion>
      <Pagination page={page} perPage={PER_PAGE} total={total} onPageChange={setPage} />
    </PageFrame>
  )
}
