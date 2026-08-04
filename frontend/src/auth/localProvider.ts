/**
 * 로컬 인증 제공자 — 브라우저 localStorage 에 계정을 둔다.
 *
 * 이것은 진짜 인증이 아니다. 검증이 클라이언트에서 일어나므로 누구든 우회할 수
 * 있고, 기기 간에 계정이 공유되지 않는다. Cognito User Pool 이 없는 환경에서
 * 화면 흐름을 끝까지 돌려보기 위한 것이며, `VITE_AUTH_PROVIDER=local` 일 때만
 * 쓰인다.
 */

import { config } from '../api/client'
import type {
  Account,
  AuthProvider,
  SignupInput,
  SignupOutcome,
  Survey,
} from './types'

const ACCOUNTS_KEY = 'tm.accounts.v1'
const SESSION_KEY = 'tm.session.v1'

interface StoredAccount extends Account {
  passwordHash: string
}

function readAccounts(): Record<string, StoredAccount> {
  try {
    const raw = localStorage.getItem(ACCOUNTS_KEY)
    return raw ? (JSON.parse(raw) as Record<string, StoredAccount>) : {}
  } catch {
    return {}
  }
}

function writeAccounts(accounts: Record<string, StoredAccount>): void {
  localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(accounts))
}

/**
 * 비밀번호를 평문으로 두지 않기 위한 최소 조치. 솔트가 계정과 같은 저장소에
 * 있으므로 오프라인 공격을 막지는 못한다. 실제 인증에서는 서버가 해야 할 일이다.
 */
async function hash(password: string, salt: string): Promise<string> {
  const bytes = new TextEncoder().encode(`${salt}:${password}`)
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

function strip(stored: StoredAccount): Account {
  const { passwordHash: _passwordHash, ...account } = stored
  return account
}

export class LocalAuthProvider implements AuthProvider {
  current(): Account | null {
    const email = localStorage.getItem(SESSION_KEY)
    if (!email) return null
    const stored = readAccounts()[email]
    return stored ? strip(stored) : null
  }

  async restore(): Promise<Account | null> {
    return this.current()
  }

  async signUp(input: SignupInput): Promise<SignupOutcome> {
    const accounts = readAccounts()
    const email = input.email.trim().toLowerCase()
    if (accounts[email]) {
      throw new Error('이미 가입된 이메일입니다.')
    }

    const account: StoredAccount = {
      email,
      name: input.name.trim(),
      birth: input.birth.trim(),
      sex: input.sex,
      tel: input.tel.trim(),
      agreements: input.agreements,
      survey: null,
      personId: config.demoPersonId,
      createdAt: new Date().toISOString(),
      passwordHash: await hash(input.password, email),
    }

    accounts[email] = account
    writeAccounts(accounts)
    localStorage.setItem(SESSION_KEY, email)
    // 로컬에는 확인할 이메일이 없다. 곧바로 로그인 상태로 넘긴다.
    return { status: 'CONFIRMED', account: strip(account) }
  }

  async confirmSignUp(): Promise<void> {
    // 확인 절차가 없다.
  }

  async resendCode(): Promise<void> {
    // 보낼 코드가 없다.
  }

  async signIn(email: string, password: string): Promise<Account> {
    const normalized = email.trim().toLowerCase()
    const stored = readAccounts()[normalized]
    // 계정 존재 여부를 오류 문구로 흘리지 않는다.
    if (!stored || stored.passwordHash !== (await hash(password, normalized))) {
      throw new Error('이메일 또는 비밀번호가 올바르지 않습니다.')
    }
    localStorage.setItem(SESSION_KEY, normalized)
    return strip(stored)
  }

  async signOut(): Promise<void> {
    localStorage.removeItem(SESSION_KEY)
  }

  async saveSurvey(survey: Survey): Promise<Account> {
    const email = localStorage.getItem(SESSION_KEY)
    const accounts = readAccounts()
    const stored = email ? accounts[email] : undefined
    if (!email || !stored) throw new Error('로그인이 필요합니다.')

    const updated: StoredAccount = { ...stored, survey }
    accounts[email] = updated
    writeAccounts(accounts)
    return strip(updated)
  }

  async idToken(): Promise<string | null> {
    return null
  }
}
