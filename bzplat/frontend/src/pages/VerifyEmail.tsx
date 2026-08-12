import { useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { MailCheck, Send } from 'lucide-react'
import CaptchaField, { type CaptchaValue } from '@/components/CaptchaField'
import AuthShell from '@/components/AuthShell'
import { Card, CardContent } from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ErrorMsg } from '@/components/ui/status'
import { apiJson, errMsg } from '@/api'

export default function VerifyEmail() {
  const nav = useNavigate()
  const [params] = useSearchParams()
  const [emailOrUsername, setEmailOrUsername] = useState(params.get('u') || '')
  const [code, setCode] = useState('')
  const [captcha, setCaptcha] = useState<CaptchaValue>({
    captcha_id: '',
    captcha_answer: '',
  })
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const onVerify = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    setMsg('')
    try {
      const d = await apiJson<{ message?: string }>('/api/auth/verify-email', 'POST', {
        email_or_username: emailOrUsername,
        code,
      })
      setMsg(d.message || '邮箱已验证')
      setTimeout(() => nav('/login'), 800)
    } catch (err) {
      setError(errMsg(err, '验证失败'))
    } finally {
      setBusy(false)
    }
  }

  const onResend = async () => {
    setBusy(true)
    setError('')
    setMsg('')
    try {
      const d = await apiJson<{ message?: string }>('/api/auth/resend-verify', 'POST', {
        email_or_username: emailOrUsername,
        captcha_id: captcha.captcha_id,
        captcha_answer: captcha.captcha_answer,
      })
      setMsg(d.message || '验证码已重新发送')
    } catch (err) {
      setError(errMsg(err, '重发失败'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthShell layout="auth-verify-email" title="验证邮箱" subtitle="输入邮件验证码完成验证；没有收到时可以重新发送。">
      <Card density="compact" className="mx-auto w-full max-w-md">
        <CardContent>
          <form onSubmit={(e) => void onVerify(e)} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="verify-account">用户名或邮箱</Label>
              <Input
                id="verify-account"
                value={emailOrUsername}
                onChange={(e) => setEmailOrUsername(e.target.value)}
                required
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="verify-code">验证码</Label>
              <Input
                id="verify-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                required
                maxLength={8}
                autoComplete="one-time-code"
                className="font-mono tracking-widest"
              />
            </div>
            {error && <ErrorMsg msg={error} />}
            {msg && <p role="status" className="text-sm text-primary">{msg}</p>}
            <Button type="submit" disabled={busy} aria-busy={busy} className="w-full gap-1.5">
              <MailCheck className="size-4" />
              {busy ? '提交中…' : '完成验证'}
            </Button>
          </form>

          <Separator className="my-4" />

          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">未收到邮件？填写验证码后重发：</p>
            <CaptchaField onChange={setCaptcha} />
            <Button
              type="button"
              variant="outline"
              disabled={busy || !emailOrUsername}
              aria-busy={busy}
              onClick={() => void onResend()}
              className="w-full gap-1.5"
            >
              <Send className="size-4" />
              重新发送验证码
            </Button>
            <p className="text-center text-sm text-muted-foreground">
              <Link to="/login" className="font-medium text-primary hover:underline">
                返回登录
              </Link>
            </p>
          </div>
        </CardContent>
      </Card>
    </AuthShell>
  )
}
