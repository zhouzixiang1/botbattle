import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import CaptchaField, { type CaptchaValue } from '../components/CaptchaField'
import PageStub from '../components/PageStub'
import { apiJson, errMsg } from '../api'

export default function Register() {
  const nav = useNavigate()
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [captcha, setCaptcha] = useState<CaptchaValue>({
    captcha_id: '',
    captcha_answer: '',
  })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await apiJson('/api/auth/register', 'POST', {
        username,
        email,
        password,
        display_name: displayName,
        captcha_id: captcha.captcha_id,
        captcha_answer: captcha.captcha_answer,
      })
      nav(`/verify-email?u=${encodeURIComponent(username || email)}`)
    } catch (err) {
      setError(errMsg(err, '注册失败'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <PageStub title="注册">
      <form onSubmit={(e) => void onSubmit(e)} className="mx-auto mt-2 max-w-md space-y-4">
        <label className="block text-sm text-slate-600">
          用户名
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            minLength={3}
            maxLength={32}
            autoComplete="username"
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none"
          />
        </label>
        <label className="block text-sm text-slate-600">
          邮箱
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoComplete="email"
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none"
          />
        </label>
        <label className="block text-sm text-slate-600">
          显示名（可选）
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            maxLength={64}
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none"
          />
        </label>
        <label className="block text-sm text-slate-600">
          密码（至少 8 位）
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={8}
            autoComplete="new-password"
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none"
          />
        </label>
        <CaptchaField onChange={setCaptcha} />
        {error && <p className="text-sm text-error-500">{error}</p>}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-lg bg-brand-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-brand-500 disabled:opacity-60"
        >
          {busy ? '注册中…' : '注册'}
        </button>
        <p className="text-center text-sm text-slate-400">
          已有账号？{' '}
          <Link to="/login" className="text-brand-600 hover:text-brand-700">
            去登录
          </Link>
        </p>
      </form>
    </PageStub>
  )
}
