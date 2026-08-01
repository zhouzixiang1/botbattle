import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { KeyRound, MailCheck } from 'lucide-react'
import CaptchaField, { type CaptchaValue } from '@/components/CaptchaField'
import PageStub from '@/components/PageStub'
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
    <PageStub title="重置密码">
      <Card className="mx-auto mt-2 max-w-md">
        <CardContent className="py-6">
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
              {msg && <p className="text-sm text-primary">{msg}</p>}
              <Button type="submit" disabled={busy} className="w-full gap-1.5">
                <KeyRound className="size-4" />
                {busy ? '发送中…' : '发送重置验证码'}
              </Button>
              <p className="text-center text-sm text-muted-foreground">
                <Link to="/login" className="font-medium text-primary hover:underline">
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
              {msg && <p className="text-sm text-primary">{msg}</p>}
              <Button type="submit" disabled={busy} className="w-full gap-1.5">
                <MailCheck className="size-4" />
                {busy ? '提交中…' : '重置密码'}
              </Button>
              <Button
                type="button"
                variant="ghost"
                onClick={() => setStep('request')}
                className="w-full text-muted-foreground"
              >
                重新发送验证码
              </Button>
            </form>
          )}
        </CardContent>
      </Card>
    </PageStub>
  )
}
