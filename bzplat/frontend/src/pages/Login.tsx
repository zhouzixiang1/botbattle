import { useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { LogIn } from 'lucide-react'
import CaptchaField, { type CaptchaValue } from '@/components/CaptchaField'
import AuthShell from '@/components/AuthShell'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ErrorMsg } from '@/components/ui/status'
import { useAuth } from '@/components/useAuth'
import { errMsg, isUnauthorized } from '@/api'

export default function Login() {
  const { login } = useAuth()
  const nav = useNavigate()
  const [params] = useSearchParams()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [captcha, setCaptcha] = useState<CaptchaValue>({ captcha_id: '', captcha_answer: '' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const sessionTip = params.get('reason') === 'expired'

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await login(username, password, captcha.captcha_id, captcha.captcha_answer)
      const from = params.get('from') || '/'
      nav(from.startsWith('/') ? from : '/')
    } catch (err) {
      setError(errMsg(err, isUnauthorized(err) ? '用户名或密码错误' : '登录失败'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthShell layout="auth-login" title="登录" subtitle="登录后上传 Bot、发起挑战并管理赛事消息">
      <Card density="compact" className="mx-auto w-full max-w-md">
        <CardContent>
          <form onSubmit={(e) => void onSubmit(e)} className="space-y-4">
            {sessionTip && (
              <p role="status" className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning-foreground">
                未登录或会话过期，请重新登录
              </p>
            )}
            <div className="space-y-1.5">
              <Label htmlFor="login-username">用户名</Label>
              <Input id="login-username" value={username} onChange={(e) => setUsername(e.target.value)} required autoComplete="username" />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="login-password">密码</Label>
              <Input id="login-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="current-password" />
            </div>
            <CaptchaField onChange={setCaptcha} />
            {error && <ErrorMsg msg={error} />}
            <Button type="submit" disabled={busy} aria-busy={busy} className="w-full gap-1.5">
              <LogIn className="size-4" />{busy ? '登录中…' : '登录'}
            </Button>
            <p className="text-center text-sm text-muted-foreground">
              没有账号？{' '}
              <Link to="/register" className="font-medium text-primary hover:underline">注册</Link>
              {' · '}
              <Link to="/reset-password" className="font-medium text-primary hover:underline">重置密码</Link>
            </p>
          </form>
        </CardContent>
      </Card>
    </AuthShell>
  )
}
