/**
 * FastAPI 백엔드에 붙는 얇은 HTTP 클라이언트.
 *
 * UI 코드가 URL 이나 HTTP 세부사항을 직접 다루지 않도록 여기서만 처리한다.
 * 삭제된 Streamlit UI 의 `api_client.py` 와 같은 역할이다.
 */

export const config = {
  baseUrl: (import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000').replace(
    /\/$/,
    '',
  ),
  useMock: import.meta.env.VITE_USE_MOCK === 'true',
  demoPersonId: Number(import.meta.env.VITE_DEMO_PERSON_ID ?? 1),
  actor: 'web-ui',
}

export class ApiError extends Error {
  /** 서버가 뜨지 않은 경우를 구분한다. UI 에서 설정 안내를 따로 띄우기 위한 플래그. */
  readonly unreachable: boolean
  readonly status: number | null

  constructor(
    message: string,
    options: { status?: number | null; unreachable?: boolean } = {},
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = options.status ?? null
    this.unreachable = options.unreachable ?? false
  }
}

/**
 * 요청에 붙일 ID 토큰을 돌려주는 함수. 인증 계층이 앱 시작 시 등록한다.
 *
 * 여기서 `auth/` 를 직접 import 하지 않는 이유는 순환 의존 때문이다.
 * `auth/provider.ts` 가 `api/client.ts` 의 config 를 쓰고 있다.
 */
let tokenSource: (() => Promise<string | null>) | null = null

export function setTokenSource(source: () => Promise<string | null>): void {
  tokenSource = source
}

type Query = Record<string, string | number | boolean | null | undefined>

function buildUrl(path: string, query?: Query): string {
  const url = new URL(config.baseUrl + path)
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === null || value === undefined || value === '') continue
      url.searchParams.set(key, String(value))
    }
  }
  return url.toString()
}

async function request<T>(
  method: string,
  path: string,
  options: { query?: Query; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  // 백엔드(`backend/api/app/auth/`)가 이 토큰을 검증한다. Cognito 미설정
  // 개방 모드에서는 토큰이 없어도 통과한다.
  const token = tokenSource ? await tokenSource() : null

  let response: Response
  try {
    response = await fetch(buildUrl(path, options.query), {
      method,
      headers: {
        Accept: 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.body === undefined
          ? {}
          : { 'Content-Type': 'application/json' }),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    })
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw new ApiError(
      `백엔드에 연결할 수 없습니다 (${config.baseUrl}). uvicorn 이 떠 있는지, VITE_API_BASE_URL 이 맞는지 확인하세요.`,
      { unreachable: true },
    )
  }

  if (!response.ok) {
    // FastAPI 는 오류를 {"detail": ...} 로 준다. 검증 오류는 detail 이 배열이다.
    let detail = `${response.status} ${response.statusText}`
    try {
      const payload = await response.json()
      if (typeof payload?.detail === 'string') detail = payload.detail
      else if (Array.isArray(payload?.detail)) {
        detail = payload.detail
          .map((item: { msg?: string }) => item?.msg ?? '')
          .filter(Boolean)
          .join(', ')
      }
    } catch {
      /* 본문이 JSON 이 아니면 상태줄을 그대로 쓴다 */
    }
    throw new ApiError(detail, { status: response.status })
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const http = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) =>
    request<T>('GET', path, { query, signal }),
  post: <T>(path: string, body?: unknown, signal?: AbortSignal) =>
    request<T>('POST', path, { body, signal }),
  patch: <T>(path: string, body?: unknown, signal?: AbortSignal) =>
    request<T>('PATCH', path, { body, signal }),
}
