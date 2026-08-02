import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowLeft, CheckCircle2 } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { useAuth } from '@/components/useAuth'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { Avatar, AvatarImage, AvatarFallback } from '@/components/ui/avatar'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { Switch } from '@/components/ui/switch'
import { EmptyState, ErrorMsg } from '@/components/ui/status'
import { apiGet, apiJson, errMsg } from '@/api'
import { gameLabel } from '@/lib/games'

interface Prefs {
  email_match_done: number
  email_followed: number
  email_contest: number
  email_comment: number
}
interface FavBot {
  id: number
  name: string
  display_name: string
  game_id: string
  owner_name?: string
  rating?: number
}

export default function Settings() {
  const { user, refresh } = useAuth()
  const [tab, setTab] = useState<'profile' | 'password' | 'notifications' | 'favorites'>('profile')
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')

  // profile 表单
  const [displayName, setDisplayName] = useState(user?.display_name || '')
  const [bio, setBio] = useState(user?.bio || '')
  // password 表单
  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  // prefs
  const [prefs, setPrefs] = useState<Prefs>({
    email_match_done: 0, email_followed: 0, email_contest: 0, email_comment: 0,
  })
  // favorites
  const [favs, setFavs] = useState<FavBot[]>([])

  useEffect(() => {
    apiGet<{ prefs: Prefs }>('/api/notification-prefs')
      .then((d) => setPrefs(d.prefs))
      .catch(() => {})
    apiGet<{ favorites: FavBot[] }>('/api/auth/me/favorites')
      .then((d) => setFavs(d.favorites || []))
      .catch(() => {})
  }, [])

  if (!user) {
    return (
      <PageStub title="设置">
        <EmptyState text="请先登录" />
      </PageStub>
    )
  }

  function saveProfile(e: React.FormEvent) {
    e.preventDefault()
    setError(''); setMsg('')
    apiJson('/api/auth/profile', 'PUT', { display_name: displayName, bio })
      .then(() => {
        if (refresh) refresh()
        setMsg('资料已保存')
      })
      .catch((e) => setError(errMsg(e)))
  }

  function uploadAvatar(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0]
    if (!f) return
    setError(''); setMsg('')
    const fd = new FormData()
    fd.append('file', f)
    apiJson('/api/auth/avatar', 'POST', fd)
      .then(() => {
        if (refresh) refresh()
        setMsg('头像已更新')
      })
      .catch((e) => setError(errMsg(e)))
  }

  function changePassword(e: React.FormEvent) {
    e.preventDefault()
    setError(''); setMsg('')
    apiJson('/api/auth/change-password', 'POST', { old_password: oldPw, new_password: newPw })
      .then(() => {
        setMsg('密码已修改，请重新登录')
        setOldPw(''); setNewPw('')
      })
      .catch((e) => setError(errMsg(e)))
  }

  function togglePref(key: keyof Prefs) {
    const next = { ...prefs, [key]: prefs[key] ? 0 : 1 }
    setPrefs(next)
    apiJson('/api/notification-prefs', 'PUT', next).catch((e) => setError(errMsg(e)))
  }

  const avatarUrl = user.avatar ? `/avatars/${user.avatar}` : ''

  return (
    <PageStub title="个人设置" subtitle="管理资料、头像与密码">
      {/* Tabs */}
      <Tabs
        value={tab}
        onValueChange={(v) => setTab(v as typeof tab)}
        className="mb-4 w-full"
      >
        <TabsList variant="line">
          <TabsTrigger value="profile">资料</TabsTrigger>
          <TabsTrigger value="password">密码</TabsTrigger>
          <TabsTrigger value="notifications">通知偏好</TabsTrigger>
          <TabsTrigger value="favorites">我的收藏</TabsTrigger>
        </TabsList>

        {/* 资料 */}
        <TabsContent value="profile">
          {error && <ErrorMsg msg={error} className="mb-3 mt-3" />}
          {msg && (
            <p className="mb-3 mt-3 flex items-center gap-1.5 text-sm text-success">
              <CheckCircle2 className="size-4" />
              {msg}
            </p>
          )}
          <form onSubmit={saveProfile} className="mx-auto max-w-md space-y-4">
            <div className="space-y-2">
              <Label>头像</Label>
              <div className="flex items-center gap-3">
                <Avatar className="size-16">
                  {avatarUrl ? (
                    <AvatarImage src={avatarUrl} alt="avatar" />
                  ) : null}
                  <AvatarFallback className="text-xl font-bold text-muted-foreground">
                    {(user.display_name || user.username).charAt(0).toUpperCase()}
                  </AvatarFallback>
                </Avatar>
                <Input
                  type="file"
                  accept="image/png,image/jpeg,image/webp,image/gif"
                  onChange={uploadAvatar}
                  className="max-w-xs text-xs text-muted-foreground"
                />
              </div>
              <p className="text-xs text-muted-foreground">png/jpeg/webp/gif，≤2MB</p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="settings-display">显示名</Label>
              <Input
                id="settings-display"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                maxLength={64}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="settings-bio">简介</Label>
              <Textarea
                id="settings-bio"
                value={bio}
                onChange={(e) => setBio(e.target.value)}
                maxLength={500}
                rows={3}
              />
            </div>
            <Button type="submit">保存</Button>
          </form>
        </TabsContent>

        {/* 密码 */}
        <TabsContent value="password">
          {error && <ErrorMsg msg={error} className="mb-3 mt-3" />}
          {msg && (
            <p className="mb-3 mt-3 flex items-center gap-1.5 text-sm text-success">
              <CheckCircle2 className="size-4" />
              {msg}
            </p>
          )}
          <form onSubmit={changePassword} className="mx-auto max-w-md space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="settings-oldpw">当前密码</Label>
              <Input
                id="settings-oldpw"
                type="password"
                value={oldPw}
                onChange={(e) => setOldPw(e.target.value)}
                required
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="settings-newpw">新密码（≥8 位）</Label>
              <Input
                id="settings-newpw"
                type="password"
                value={newPw}
                onChange={(e) => setNewPw(e.target.value)}
                required
                minLength={8}
              />
            </div>
            <Button type="submit">修改密码</Button>
            <p className="text-xs text-muted-foreground">修改密码后会清除所有登录会话，需重新登录。</p>
          </form>
        </TabsContent>

        {/* 通知偏好 */}
        <TabsContent value="notifications">
          {error && <ErrorMsg msg={error} className="mb-3 mt-3" />}
          {msg && (
            <p className="mb-3 mt-3 flex items-center gap-1.5 text-sm text-success">
              <CheckCircle2 className="size-4" />
              {msg}
            </p>
          )}
          <div className="mx-auto max-w-md space-y-3">
            <p className="text-sm text-muted-foreground">
              站内通知始终开启；以下控制是否同时发送邮件提醒（需管理员配置 SMTP）。
            </p>
            {([
              ['email_match_done', '对局完成'],
              ['email_followed', '被关注'],
              ['email_contest', '赛事阶段变化'],
              ['email_comment', '被评论'],
            ] as const).map(([key, label]) => (
              <div key={key} className="flex items-center justify-between gap-2 text-sm">
                <span className="text-foreground">{label} 邮件提醒</span>
                <Switch
                  checked={!!prefs[key]}
                  onCheckedChange={() => togglePref(key)}
                />
              </div>
            ))}
          </div>
        </TabsContent>

        {/* 我的收藏 */}
        <TabsContent value="favorites">
          {error && <ErrorMsg msg={error} className="mb-3 mt-3" />}
          {msg && (
            <p className="mb-3 mt-3 flex items-center gap-1.5 text-sm text-success">
              <CheckCircle2 className="size-4" />
              {msg}
            </p>
          )}
          <div>
            {favs.length === 0 ? (
              <EmptyState text="暂无收藏的 Bot" />
            ) : (
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {favs.map((b) => (
                  <Link key={b.id} to={`/bot/${b.id}`}>
                    <Card className="py-4 transition-colors hover:border-primary/40">
                      <div className="flex items-center justify-between px-4">
                        <span className="font-medium text-foreground">{b.display_name || b.name}</span>
                        <Badge variant="secondary">{gameLabel(b.game_id)}</Badge>
                      </div>
                      <p className="mt-1 px-4 text-xs text-muted-foreground">
                        @{b.name}{b.owner_name ? ` · ${b.owner_name}` : ''}{b.rating != null ? ` · ${Number(b.rating).toFixed(0)}` : ''}
                      </p>
                    </Card>
                  </Link>
                ))}
              </div>
            )}
          </div>
        </TabsContent>
      </Tabs>

      <p className="mt-6">
        <Button asChild variant="link" className="h-auto p-0">
          <Link to={`/user/${encodeURIComponent(user.username)}`}>
            <ArrowLeft className="size-4" />
            返回我的主页
          </Link>
        </Button>
      </p>
    </PageStub>
  )
}
