import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import CaptchaField, { type CaptchaValue } from '../components/CaptchaField'
import PageStub from '../components/PageStub'
import { apiJson, errMsg } from '../api'

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
      {step === 'request' ? (
        <form onSubmit={(e) => void onRequest(e)} className="mx-auto mt-2 max-w-md space-y-4">
          <label className="block text-sm text-slate-600">
            用户名或邮箱
            <input
              value={emailOrUsername}
              onChange={(e) => setEmailOrUsername(e.target.value)}
              required
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none"
            />
          </label>
          <CaptchaField onChange={setCaptcha} />
          {error && <p className="text-sm text-error-500">{error}</p>}
          {msg && <p className="text-sm text-brand-700">{msg}</p>}
          <button
            type="submit"
            disabled={busy}
            className="w-full rounded-lg bg-brand-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-brand-500 disabled:opacity-60"
          >
            {busy ? '发送中…' : '发送重置验证码'}
          </button>
          <p className="text-center text-sm text-slate-400">
            <Link to="/login" className="text-brand-600 hover:text-brand-700">
              返回登录
            </Link>
          </p>
        </form>
      ) : (
        <form onSubmit={(e) => void onReset(e)} className="mx-auto mt-2 max-w-md space-y-4">
          <p className="text-sm text-slate-400">请输入邮件中的验证码与新密码。</p>
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
            邮件验证码
            <input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              required
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 font-mono tracking-widest text-slate-800 focus:border-brand-400 focus:outline-none"
            />
          </label>
          <label className="block text-sm text-slate-600">
            新密码（至少 8 位）
            <input
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              required
              minLength={8}
              autoComplete="new-password"
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none"
            />
          </label>
          {error && <p className="text-sm text-error-500">{error}</p>}
          {msg && <p className="text-sm text-brand-700">{msg}</p>}
          <button
            type="submit"
            disabled={busy}
            className="w-full rounded-lg bg-brand-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-brand-500 disabled:opacity-60"
          >
            {busy ? '提交中…' : '重置密码'}
          </button>
          <button
            type="button"
            onClick={() => setStep('request')}
            className="w-full text-sm text-slate-400 hover:text-brand-700"
          >
            重新发送验证码
          </button>
        </form>
      )}
    </PageStub>
  )
}
