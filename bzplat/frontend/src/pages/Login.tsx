import { useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import CaptchaField, { type CaptchaValue } from '../components/CaptchaField'
import PageStub from '../components/PageStub'
import { useAuth } from '../components/useAuth'
import { errMsg, isUnauthorized } from '../api'

export default function Login() {
  const { login } = useAuth()
  const nav = useNavigate()
  const [params] = useSearchParams()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [captcha, setCaptcha] = useState<CaptchaValue>({
    captcha_id: '',
    captcha_answer: '',
  })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // 仅会话过期跳转时提示；主动点「登录」不显示
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
      // 登录接口的 401 是「用户名或密码错误」等业务错误，不是会话提示
      setError(errMsg(err, isUnauthorized(err) ? '用户名或密码错误' : '登录失败'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <PageStub title="登录">
      <form onSubmit={(e) => void onSubmit(e)} className="mx-auto mt-2 max-w-md space-y-4">
        {sessionTip && (
          <p className="rounded-lg border border-amber-500/30 bg-amber-50 px-3 py-2 text-sm text-amber-700">
            未登录或会话过期，请重新登录
          </p>
        )}
        <label className="block text-sm text-slate-600">
          用户名
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            autoComplete="username"
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none"
          />
        </label>
        <label className="block text-sm text-slate-600">
          密码
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            autoComplete="current-password"
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
          {busy ? '登录中…' : '登录'}
        </button>
        <p className="text-center text-sm text-slate-400">
          没有账号？{' '}
          <Link to="/register" className="text-brand-600 hover:text-brand-700">
            注册
          </Link>
          {' · '}
          <Link to="/reset-password" className="text-brand-600 hover:text-brand-700">
            重置密码
          </Link>
        </p>
      </form>
    </PageStub>
  )
}
