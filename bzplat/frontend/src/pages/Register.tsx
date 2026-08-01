import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { UserPlus } from 'lucide-react'
import CaptchaField, { type CaptchaValue } from '@/components/CaptchaField'
import PageStub from '@/components/PageStub'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ErrorMsg } from '@/components/ui/status'
import { apiJson, errMsg } from '@/api'

export default function Register() {
  const nav = useNavigate()
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [captcha, setCaptcha] = useState<CaptchaValue>({ captcha_id: '', captcha_answer: '' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await apiJson('/api/auth/register', 'POST', {
        username, email, password, display_name: displayName,
        captcha_id: captcha.captcha_id, captcha_answer: captcha.captcha_answer,
      })
      nav(`/verify-email?u=${encodeURIComponent(username || email)}`)
    } catch (err) {
      setError(errMsg(err, '注册失败'))
    } finally {
      setBusy(false)
    }
  }

  const inputCls = 'mt-1.5'

  return (
    <PageStub title="注册">
      <Card className="mx-auto mt-2 max-w-md">
        <CardContent className="py-6">
          <form onSubmit={(e) => void onSubmit(e)} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="reg-username">用户名</Label>
              <Input id="reg-username" className={inputCls} value={username} onChange={(e) => setUsername(e.target.value)} required minLength={3} maxLength={32} autoComplete="username" />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="reg-email">邮箱</Label>
              <Input id="reg-email" type="email" className={inputCls} value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="reg-display">显示名（可选）</Label>
              <Input id="reg-display" className={inputCls} value={displayName} onChange={(e) => setDisplayName(e.target.value)} maxLength={64} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="reg-password">密码（至少 8 位）</Label>
              <Input id="reg-password" type="password" className={inputCls} value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8} autoComplete="new-password" />
            </div>
            <CaptchaField onChange={setCaptcha} />
            {error && <ErrorMsg msg={error} />}
            <Button type="submit" disabled={busy} className="w-full gap-1.5">
              <UserPlus className="size-4" />{busy ? '注册中…' : '注册'}
            </Button>
            <p className="text-center text-sm text-muted-foreground">
              已有账号？{' '}
              <Link to="/login" className="font-medium text-primary hover:underline">去登录</Link>
            </p>
          </form>
        </CardContent>
      </Card>
    </PageStub>
  )
}
