import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { KeyRound, MailCheck } from 'lucide-react'
import { toast } from 'sonner'
import CaptchaField, { type CaptchaValue } from '@/components/CaptchaField'
import AuthShell from '@/components/AuthShell'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ErrorMsg } from '@/components/ui/status'
import { apiJson, errMsg } from '@/api'

export default function ResetPassword() {
  const nav = useNavigate()
  const [emailOrUsername, setEmailOrUsername] = useState('')
  const [code, setCode] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [captcha, setCaptcha] = useState<CaptchaValue>({
    captcha_id: '',
    captcha_answer: '',
  })
  const [step, setStep] = useState<'request' | 'reset'>('request')
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const onRequest = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    setMsg('')
    try {
      const d = await apiJson<{ message?: string }>('/api/auth/request-reset', 'POST', {
        email_or_username: emailOrUsername,
        captcha_id: captcha.captcha_id,
        captcha_answer: captcha.captcha_answer,
      })
      setMsg(d.message || '若账号存在，重置验证码已发送')
      setStep('reset')
    } catch (err) {
      setError(errMsg(err, '请求失败'))
    } finally {
      setBusy(false)
    }
  }

  // 后端图形验证码一次性消费；重发成功后重挂验证码组件换新码，连续重发才可用。
  const [captchaEpoch, setCaptchaEpoch] = useState(0)
  const onResend = async () => {
    setBusy(true)
    setError('')
    try {
      const d = await apiJson<{ message?: string }>('/api/auth/request-reset', 'POST', {
        email_or_username: emailOrUsername,
        captcha_id: captcha.captcha_id,
        captcha_answer: captcha.captcha_answer,
      })
      toast.success(d.message || '验证码已重新发送')
      setCaptchaEpoch((value) => value + 1)
    } catch (err) {
      setError(errMsg(err, '重发失败'))
    } finally {
      setBusy(false)
    }
  }

  const onReset = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    setMsg('')
    try {
      const d = await apiJson<{ message?: string }>('/api/auth/reset-password', 'POST', {
        email_or_username: emailOrUsername,
        code,
        new_password: newPassword,
      })
      setMsg(d.message || '密码已重置')
      setTimeout(() => nav('/login'), 800)
    } catch (err) {
      setError(errMsg(err, '重置失败'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthShell layout="auth-reset-password" title="重置密码" subtitle={step === 'request' ? '先获取邮件验证码。' : '输入验证码，设置新密码。'}>
      <Card density="compact" className="mx-auto w-full max-w-md">
        <CardContent>
          {step === 'request' ? (
            <form onSubmit={(e) => void onRequest(e)} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="reset-account">用户名或邮箱</Label>
                <Input
                  id="reset-account"
                  value={emailOrUsername}
                  onChange={(e) => setEmailOrUsername(e.target.value)}
                  required
                />
              </div>
              <CaptchaField onChange={setCaptcha} />
              {error && <ErrorMsg msg={error} />}
              {msg && <p role="status" className="text-sm text-primary">{msg}</p>}
              <Button type="submit" disabled={busy} aria-busy={busy} className="w-full gap-1.5">
                <KeyRound className="size-4" />
                {busy ? '发送中…' : '发送重置验证码'}
              </Button>
              <p className="text-center text-sm text-muted-foreground">
                <Link to="/login" className="inline-flex min-h-11 items-center font-medium text-primary hover:underline">
                  返回登录
                </Link>
              </p>
            </form>
          ) : (
            <form onSubmit={(e) => void onReset(e)} className="space-y-4">
              <p className="text-sm text-muted-foreground">请输入邮件中的验证码与新密码。</p>
              <div className="space-y-1.5">
                <Label htmlFor="reset-account-2">用户名或邮箱</Label>
                <Input
                  id="reset-account-2"
                  value={emailOrUsername}
                  onChange={(e) => setEmailOrUsername(e.target.value)}
                  required
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="reset-code">邮件验证码</Label>
                <Input
                  id="reset-code"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  required
                  autoComplete="one-time-code"
                  className="font-mono tracking-widest"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="reset-newpw">新密码（至少 8 位）</Label>
                <Input
                  id="reset-newpw"
                  type="password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  required
                  minLength={8}
                  autoComplete="new-password"
                />
              </div>
              {error && <ErrorMsg msg={error} />}
              {msg && <p role="status" className="text-sm text-primary">{msg}</p>}
              <Button type="submit" disabled={busy} aria-busy={busy} className="w-full gap-1.5">
                <MailCheck className="size-4" />
                {busy ? '提交中…' : '重置密码'}
              </Button>
              <div className="space-y-2 rounded-lg border border-dashed border-border px-3 py-3">
                <p className="text-xs text-muted-foreground">没有收到邮件？输入图形验证码后可重新发送。</p>
                <CaptchaField key={captchaEpoch} onChange={setCaptcha} />
                <Button
                  type="button"
                  variant="outline"
                  disabled={busy || !emailOrUsername}
                  aria-busy={busy}
                  onClick={() => void onResend()}
                  className="w-full gap-1.5"
                >
                  重新发送验证码
                </Button>
              </div>
            </form>
          )}
        </CardContent>
      </Card>
    </AuthShell>
  )
}
