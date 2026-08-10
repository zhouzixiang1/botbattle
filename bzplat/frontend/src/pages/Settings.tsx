import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowLeft, CheckCircle2, Star } from 'lucide-react'
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
import { fmtRating } from '@/lib/format'

interface Prefs {
  email_match_done: boolean
  email_followed: boolean
  email_contest: boolean
  email_comment: boolean
}
const PREF_KEYS = [
  'email_match_done',
  'email_followed',
  'email_contest',
  'email_comment',
] as const satisfies readonly (keyof Prefs)[]
const DEFAULT_PREFS: Prefs = {
  email_match_done: false,
  email_followed: false,
  email_contest: false,
  email_comment: false,
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
  const { user, refresh, logout } = useAuth()
  const navigate = useNavigate()
  const [tab, setTab] = useState<'profile' | 'password' | 'notifications' | 'favorites'>('profile')
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')

  // profile 表单
  const [displayName, setDisplayName] = useState(user?.display_name || '')
  const [bio, setBio] = useState(user?.bio || '')
  const [realName, setRealName] = useState(user?.real_name || '')
  const [phone, setPhone] = useState(user?.phone || '')
  const [school, setSchool] = useState(user?.school || '')
  const [studentId, setStudentId] = useState(user?.student_id || '')
  // password 表单
  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  // prefs
  const [prefs, setPrefs] = useState<Prefs>({ ...DEFAULT_PREFS })
  const desiredPrefsRef = useRef<Prefs>({ ...DEFAULT_PREFS })
  const confirmedPrefsRef = useRef<Prefs>({ ...DEFAULT_PREFS })
  const prefRevisionsRef = useRef<Record<keyof Prefs, number>>({
    email_match_done: 0,
    email_followed: 0,
    email_contest: 0,
    email_comment: 0,
  })
  const prefInFlightRef = useRef<Partial<Record<keyof Prefs, boolean>>>({})
  const prefEpochRef = useRef(0)
  const [pendingPrefs, setPendingPrefs] = useState<Partial<Record<keyof Prefs, boolean>>>({})
  // favorites
  const [favs, setFavs] = useState<FavBot[]>([])
  const [avatarFileName, setAvatarFileName] = useState('')

  useEffect(() => {
    const epoch = ++prefEpochRef.current
    desiredPrefsRef.current = { ...DEFAULT_PREFS }
    confirmedPrefsRef.current = { ...DEFAULT_PREFS }
    prefInFlightRef.current = {}
    for (const key of PREF_KEYS) prefRevisionsRef.current[key] += 1
    setPrefs({ ...DEFAULT_PREFS })
    setPendingPrefs({})
    if (!user) return
    const revisionsAtStart = { ...prefRevisionsRef.current }
    apiGet<{ prefs: Prefs }>('/api/notification-prefs')
      .then((d) => {
        if (prefEpochRef.current !== epoch) return
        setPrefs((current) => {
          const next = { ...current }
          for (const key of PREF_KEYS) {
            // A user action after this GET started is authoritative.  The stale
            // snapshot may initialize untouched switches but must never undo it.
            if (prefRevisionsRef.current[key] !== revisionsAtStart[key]) continue
            const value = Boolean(d.prefs[key])
            confirmedPrefsRef.current[key] = value
            desiredPrefsRef.current[key] = value
            next[key] = value
          }
          return next
        })
      })
      .catch(() => {})
  }, [user?.id])

  useEffect(() => {
    if (!user) return
    apiGet<{ favorites: FavBot[] }>('/api/auth/me/favorites')
      .then((d) => setFavs(d.favorites || []))
      .catch(() => {})
  }, [user?.id])

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
    apiJson('/api/auth/profile', 'PUT', {
      display_name: displayName, bio,
      real_name: realName, phone, school, student_id: studentId,
    })
      .then(() => {
        if (refresh) refresh()
        setMsg('资料已保存')
      })
      .catch((e) => setError(errMsg(e)))
  }

  function uploadAvatar(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0]
    setAvatarFileName(f?.name || '')
    if (!f) return
    setError(''); setMsg('')
    const fd = new FormData()
    fd.append('file', f)
    apiJson('/api/auth/avatar', 'POST', fd)
      .then(() => {
        if (refresh) refresh()
        setAvatarVer((v) => v + 1) // 刷新 <img> src 缓存（文件名不变）
        setMsg('头像已更新')
      })
      .catch((e) => setError(errMsg(e)))
  }

  function changePassword(e: React.FormEvent) {
    e.preventDefault()
    setError(''); setMsg('')
    apiJson('/api/auth/change-password', 'POST', { old_password: oldPw, new_password: newPw })
      .then(async () => {
        // 后端已清除所有会话——前端必须同步清理本地 token/user，否则停留在无效会话上后续操作全 401。
        setMsg('密码已修改，正在跳转登录…')
        setOldPw(''); setNewPw('')
        await logout()
        navigate('/login', { replace: true })
      })
      .catch((e) => setError(errMsg(e)))
  }

  function applyPrefValue(key: keyof Prefs, value: boolean) {
    desiredPrefsRef.current = { ...desiredPrefsRef.current, [key]: value }
    setPrefs((current) => ({ ...current, [key]: value }))
  }

  function flushPrefQueue(key: keyof Prefs, epoch: number) {
    if (prefInFlightRef.current[key]) return
    prefInFlightRef.current[key] = true
    setPendingPrefs((current) => ({ ...current, [key]: true }))

    void (async () => {
      while (prefEpochRef.current === epoch) {
        const revision = prefRevisionsRef.current[key]
        const target = desiredPrefsRef.current[key]
        try {
          const { prefs: saved } = await apiJson<{ prefs: Prefs }>(
            '/api/notification-prefs', 'PUT', { [key]: target },
          )
          if (prefEpochRef.current !== epoch) return
          const savedValue = Boolean(saved[key])
          confirmedPrefsRef.current[key] = savedValue
          if (prefRevisionsRef.current[key] === revision) {
            applyPrefValue(key, savedValue)
            setError('')
            return
          }
          // More clicks arrived while this request was in flight.  If the last
          // desired value already equals the committed value, the queue is done;
          // otherwise loop and persist only the newest value, in order.
          if (desiredPrefsRef.current[key] === savedValue) return
        } catch (requestError) {
          if (prefEpochRef.current !== epoch) return
          if (prefRevisionsRef.current[key] !== revision) continue
          setError(errMsg(requestError))
          // The request outcome is unknown.  Refresh the one public source of
          // truth before rolling back the optimistic switch.
          try {
            const refreshed = await apiGet<{ prefs: Prefs }>('/api/notification-prefs')
            if (
              prefEpochRef.current !== epoch ||
              prefRevisionsRef.current[key] !== revision
            ) return
            const serverValue = Boolean(refreshed.prefs[key])
            confirmedPrefsRef.current[key] = serverValue
            applyPrefValue(key, serverValue)
          } catch {
            if (
              prefEpochRef.current === epoch &&
              prefRevisionsRef.current[key] === revision
            ) applyPrefValue(key, confirmedPrefsRef.current[key])
          }
          return
        }
      }
    })().finally(() => {
      if (prefEpochRef.current !== epoch) return
      prefInFlightRef.current[key] = false
      setPendingPrefs((current) => ({ ...current, [key]: false }))
      // A click can land between the last response and this finally callback.
      if (desiredPrefsRef.current[key] !== confirmedPrefsRef.current[key]) {
        flushPrefQueue(key, epoch)
      }
    })
  }

  function togglePref(key: keyof Prefs, checked: boolean) {
    const epoch = prefEpochRef.current
    prefRevisionsRef.current[key] += 1
    applyPrefValue(key, checked)
    // Different keys may save concurrently; one key is serialized/coalesced so
    // the server and UI both end at the user's last click.
    flushPrefQueue(key, epoch)
  }

  const [avatarVer, setAvatarVer] = useState(0)
  // 后端覆盖头像时文件名不变（<uid>.<ext>），加 cache-buster 防浏览器显示旧图。
  const avatarUrl = user.avatar ? `/avatars/${user.avatar}?v=${avatarVer}` : ''

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
                <label className="inline-flex max-w-xs cursor-pointer items-center gap-2 rounded-md border border-input bg-background px-3 py-2 text-xs text-muted-foreground transition-colors hover:bg-accent">
                  <span className="shrink-0 font-medium text-foreground">选择文件</span>
                  <span className="min-w-0 truncate">{avatarFileName || '未选择文件'}</span>
                  <input
                    type="file"
                    accept="image/png,image/jpeg,image/webp,image/gif"
                    onChange={uploadAvatar}
                    className="sr-only"
                  />
                </label>
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
            {/* 实名信息（可选，报名要求实名的赛事需要） */}
            <div className="rounded-lg border border-border p-3 space-y-3">
              <p className="text-sm font-medium text-foreground">实名信息（选填）</p>
              <p className="text-xs text-muted-foreground">报名「要求实名」的赛事时需要填写完整。信息不公开展示。</p>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label htmlFor="settings-realname">姓名</Label>
                  <Input id="settings-realname" value={realName} onChange={(e) => setRealName(e.target.value)} maxLength={32} />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="settings-phone">手机号</Label>
                  <Input id="settings-phone" value={phone} onChange={(e) => setPhone(e.target.value)} maxLength={20} placeholder="13800138000" />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="settings-school">学校</Label>
                  <Input id="settings-school" value={school} onChange={(e) => setSchool(e.target.value)} maxLength={64} />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="settings-studentid">学号</Label>
                  <Input id="settings-studentid" value={studentId} onChange={(e) => setStudentId(e.target.value)} maxLength={32} />
                </div>
              </div>
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
                  aria-label={`${label}邮件提醒`}
                  aria-busy={Boolean(pendingPrefs[key])}
                  checked={prefs[key]}
                  onCheckedChange={(checked) => togglePref(key, checked)}
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
              <EmptyState text="暂无收藏的 Bot" icon={<Star className="size-7 opacity-40" />} />
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
                        @{b.name}{b.owner_name ? ` · ${b.owner_name}` : ''}{b.rating != null ? ` · ${fmtRating(b.rating)}` : ''}
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
