import { useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import CaptchaField, { type CaptchaValue } from '../components/CaptchaField'
import PageStub from '../components/PageStub'
import { apiJson, errMsg } from '../api'

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
    <PageStub title="验证邮箱">
      <p className="mb-4 text-sm text-slate-400">请输入邮件中的 6 位验证码完成邮箱验证。</p>
      <form onSubmit={(e) => void onVerify(e)} className="mx-auto max-w-md space-y-4">
        <label className="block text-sm text-slate-600">
          用户名或邮箱
          <input
            value={emailOrUsername}
            onChange={(e) => setEmailOrUsername(e.target.value)}
            required
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none"
          />
        </label>
        <label className="block text-sm text-slate-600">
          验证码
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            required
            maxLength={8}
            autoComplete="one-time-code"
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 font-mono tracking-widest text-slate-800 focus:border-brand-400 focus:outline-none"
          />
        </label>
        {error && <p className="text-sm text-error-500">{error}</p>}
        {msg && <p className="text-sm text-brand-700">{msg}</p>}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-lg bg-brand-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-brand-500 disabled:opacity-60"
        >
          {busy ? '提交中…' : '完成验证'}
        </button>
      </form>

      <div className="mx-auto mt-8 max-w-md space-y-3 border-t border-slate-200 pt-6">
        <p className="text-sm text-slate-400">未收到邮件？填写验证码后重发：</p>
        <CaptchaField onChange={setCaptcha} />
        <button
          type="button"
          disabled={busy || !emailOrUsername}
          onClick={() => void onResend()}
          className="w-full rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm text-slate-700 hover:bg-slate-100 disabled:opacity-60"
        >
          重新发送验证码
        </button>
        <p className="text-center text-sm text-slate-500">
          <Link to="/login" className="text-brand-600 hover:text-brand-700">
            返回登录
          </Link>
        </p>
      </div>
    </PageStub>
  )
}
