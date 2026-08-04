import type { ChangeEvent, ReactNode } from 'react'

import { ApiError } from '../api/client'

/** 시안 전반에서 반복되는 작은 대문자 라벨. */
export function Kicker({ children }: { children: ReactNode }) {
  return <div className="kicker">{children}</div>
}

export function Field({
  label,
  id,
  children,
}: {
  label: string
  id?: string
  children: ReactNode
}) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children}
    </div>
  )
}

export function TextInput({
  id,
  value,
  onChange,
  placeholder,
  type = 'text',
  autoComplete,
  minHeight = 42,
}: {
  id?: string
  value: string
  onChange: (value: string) => void
  placeholder?: string
  type?: string
  autoComplete?: string
  minHeight?: number
}) {
  return (
    <input
      id={id}
      className="input"
      type={type}
      value={value}
      autoComplete={autoComplete}
      placeholder={placeholder}
      style={{ minHeight }}
      onChange={(event: ChangeEvent<HTMLInputElement>) => onChange(event.target.value)}
    />
  )
}

/** 체크박스. ds 의 .radio 를 각진 dot 으로 재사용한다 (시안과 동일). */
export function Check({
  checked,
  onChange,
  children,
  fontSize = 14,
}: {
  checked: boolean
  onChange: (checked: boolean) => void
  children: ReactNode
  fontSize?: number
}) {
  return (
    <label className="radio" style={{ gap: 'var(--space-2)' }}>
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="dot" style={{ borderRadius: 'var(--radius-sm)' }} />
      <span style={{ fontSize }}>{children}</span>
    </label>
  )
}

export function Radio({
  name,
  checked,
  onChange,
  children,
}: {
  name: string
  checked: boolean
  onChange: () => void
  children: ReactNode
}) {
  return (
    <label className="radio">
      <input type="radio" name={name} checked={checked} onChange={onChange} />
      <span className="dot" />
      <span>{children}</span>
    </label>
  )
}

export function Segmented<T extends string>({
  name,
  options,
  value,
  onChange,
  minHeight = 40,
  grow = false,
}: {
  name: string
  options: readonly T[]
  value: T | ''
  onChange: (value: T) => void
  minHeight?: number
  grow?: boolean
}) {
  return (
    <div className="seg" style={grow ? { width: '100%' } : undefined}>
      {options.map((option) => (
        <label
          key={option}
          className="seg-opt"
          style={{
            minHeight,
            ...(grow
              ? { flex: 1, justifyContent: 'center' }
              : { paddingInline: 'var(--space-4)' }),
          }}
        >
          <input
            type="radio"
            name={name}
            checked={value === option}
            onChange={() => onChange(option)}
          />
          <span>{option}</span>
        </label>
      ))}
    </div>
  )
}

export function Chips({
  options,
  selected,
  onToggle,
}: {
  options: readonly string[]
  selected: readonly string[]
  onToggle: (option: string) => void
}) {
  return (
    <div className="inline-wrap">
      {options.map((option) => {
        const on = selected.includes(option)
        return (
          <button
            key={option}
            type="button"
            className={on ? 'chip on' : 'chip'}
            aria-pressed={on}
            onClick={() => onToggle(option)}
          >
            {option}
          </button>
        )
      })}
    </div>
  )
}

/** 진행 단계 막대. current 는 0-base. */
export function Steps({
  total,
  current,
  wide = false,
}: {
  total: number
  current: number
  wide?: boolean
}) {
  return (
    <div className={wide ? 'steps steps-wide' : 'steps'}>
      {Array.from({ length: total }, (_, index) => (
        <div
          key={index}
          className={
            index < current ? 'step done' : index === current ? 'step current' : 'step'
          }
        />
      ))}
    </div>
  )
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null
  const message = error instanceof Error ? error.message : String(error)
  const unreachable = error instanceof ApiError && error.unreachable
  return (
    <div className="banner banner-warn" role="alert" style={{ marginTop: 'var(--space-3)' }}>
      {message}
      {unreachable ? (
        <div style={{ marginTop: 6, fontSize: 12.5 }}>
          데이터셋 없이 화면만 보려면 <code>.env</code> 에{' '}
          <code>VITE_USE_MOCK=true</code> 를 두고 다시 실행하세요.
        </div>
      ) : null}
    </div>
  )
}

export function Loading({ label = '불러오는 중입니다' }: { label?: string }) {
  return (
    <p className="muted" style={{ fontSize: 13.5 }} aria-live="polite">
      {label}…
    </p>
  )
}
