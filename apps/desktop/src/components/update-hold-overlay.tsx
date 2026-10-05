import { useStore } from '@nanostores/react'
import { useEffect, useId, useState } from 'react'

import { Button } from '@/components/ui/button'
import type { DesktopUpdateHold } from '@/global'
import { useI18n } from '@/i18n'
import { AlertTriangle, FileText, Loader2, Power, RefreshCw } from '@/lib/icons'
import { $desktopBoot } from '@/store/boot'

type Busy = 'quit' | 'recheck' | 'start' | null

const formatTime = (ms: number) =>
  new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })

/**
 * The blocked boot screen (review R8 D3). While an earlier update's leftover
 * process holds the install — or the update helper cannot tell who owns it —
 * the local backend does not start, ever, on a timer. The user sees what holds
 * the install and can check again or quit; the only way through while the
 * hold lasts is an explicit, confirmed Start anyway that main logs. Main
 * starts the backend by itself as soon as the hold ends.
 *
 * A hold only a pool/profile backend meets while Hermes is already running
 * (`scope: 'background'`, review R9-6) is a non-blocking banner instead: the
 * working app stays usable, and Check again / Start anyway act on that hold.
 */
export function UpdateHoldOverlay() {
  const boot = useStore($desktopBoot)
  const { t } = useI18n()
  const titleId = useId()
  const [busy, setBusy] = useState<Busy>(null)
  const [confirming, setConfirming] = useState(false)
  const [refused, setRefused] = useState(false)
  const hold: DesktopUpdateHold | null = boot.error ? null : (boot.updateHold ?? null)
  const holdId = hold?.holdId ?? null
  const checkedAt = hold?.checkedAt ?? null

  // A new check landed: Check again is done.
  useEffect(() => {
    setBusy(current => (current === 'recheck' ? null : current))
  }, [checkedAt])

  // A different hold is a different decision: drop a pending confirmation.
  useEffect(() => {
    setConfirming(false)
    setBusy(null)
  }, [holdId])

  if (!hold) {
    return null
  }

  const copy = t.boot.updateHold
  const unverified = hold.verdict !== 'held'

  const recheck = async () => {
    setRefused(false)
    setBusy('recheck')
    const result = await window.hermesDesktop?.updateHold.recheck().catch(() => null)

    if (!result?.ok) {
      setBusy(null)
    }
  }

  const quit = async () => {
    setBusy('quit')
    await window.hermesDesktop?.updateHold.quit().catch(() => undefined)
  }

  const startAnyway = async () => {
    setBusy('start')

    const result = await window.hermesDesktop?.updateHold
      .startAnyway({ holdId: hold.holdId, confirmed: true })
      .catch(() => null)

    if (!result?.ok) {
      setRefused(true)
      setConfirming(false)
      setBusy(null)
    }
  }

  const openLogs = () => void window.hermesDesktop?.revealLogs().catch(() => undefined)

  const detail = unverified ? copy.unverified : hold.ownerPid ? copy.heldByProcess(hold.ownerPid) : copy.heldUnknown
  const times = `${copy.since(formatTime(hold.since))} · ${copy.lastChecked(formatTime(hold.checkedAt))}`

  const confirmButtons = (
    <div className="flex flex-wrap gap-2">
      <Button disabled={Boolean(busy)} onClick={() => setConfirming(false)} variant="secondary">
        {copy.confirmKeepWaiting}
      </Button>
      <Button disabled={Boolean(busy)} onClick={() => void startAnyway()} variant="destructive">
        {busy === 'start' ? <Loader2 className="animate-spin" /> : null}
        {copy.confirmStart}
      </Button>
    </div>
  )

  const askStartAnyway = () => {
    setRefused(false)
    setConfirming(true)
  }

  if (hold.scope === 'background') {
    return (
      <section
        aria-labelledby={titleId}
        className="fixed top-[calc(var(--titlebar-height,34px)+0.75rem)] right-4 z-(--z-over-modal) w-[min(24rem,calc(100vw-2rem))] overflow-hidden rounded-xl border border-amber-500/30 bg-(--ui-chat-bubble-background) shadow-nous"
        data-testid="update-hold-banner"
        role="region"
      >
        <div className="flex items-start gap-3 px-4 pt-3">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-500" />
          <div className="min-w-0">
            <h2 className="text-[0.8125rem] font-semibold tracking-tight" id={titleId}>
              {confirming ? copy.backgroundConfirmTitle : copy.backgroundTitle}
            </h2>
            <p className="mt-1 text-xs leading-5 text-(--ui-text-tertiary)">
              {confirming ? copy.confirmBody : copy.backgroundDescription}
            </p>
            {confirming ? null : (
              <p className="mt-1 text-xs text-muted-foreground" data-testid="update-hold-detail">
                {detail} {times}
              </p>
            )}
          </div>
        </div>

        <div className="grid gap-2 px-4 py-3">
          {refused ? <p className="text-xs text-destructive">{copy.startAnywayRefused}</p> : null}
          {confirming ? (
            confirmButtons
          ) : (
            <div className="flex flex-wrap gap-2">
              <Button disabled={Boolean(busy)} onClick={() => void recheck()} size="sm">
                {busy === 'recheck' ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                {copy.checkAgain}
              </Button>
              <Button onClick={openLogs} size="sm" variant="ghost">
                <FileText />
                {copy.openLogs}
              </Button>
              <Button className="ml-auto" disabled={Boolean(busy)} onClick={askStartAnyway} size="sm" variant="ghost">
                {copy.startAnyway}
              </Button>
            </div>
          )}
        </div>
      </section>
    )
  }

  return (
    <div
      aria-labelledby={titleId}
      aria-modal="true"
      className="fixed inset-0 z-(--z-setup) flex items-center justify-center bg-(--ui-chat-surface-background) p-6"
      // Masks the whole app while the boot is blocked — must stay filled under
      // window glass. Contract: `[data-glass-opaque]` in styles.css.
      data-glass-opaque=""
      data-testid="update-hold-overlay"
      role="alertdialog"
    >
      <div className="relative w-full max-w-[40rem] overflow-hidden rounded-xl border border-(--stroke-nous) bg-(--ui-chat-bubble-background) shadow-nous">
        <div className="flex items-start gap-3 px-5 py-4">
          <AlertTriangle className="mt-0.5 size-5 shrink-0 text-amber-500" />
          <div>
            <h2 className="text-[0.9375rem] font-semibold tracking-tight" id={titleId}>
              {confirming ? copy.confirmTitle : unverified ? copy.titleUnverified : copy.title}
            </h2>
            <p className="mt-1 text-[0.8125rem] leading-5 text-(--ui-text-tertiary)">
              {confirming ? copy.confirmBody : copy.description}
            </p>
          </div>
        </div>

        <div className="grid gap-4 p-5 pt-0">
          <div
            className="rounded-2xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-xs text-(--ui-text-secondary)"
            data-testid="update-hold-detail"
          >
            <p>{detail}</p>
            <p className="mt-1 text-muted-foreground">{times}</p>
          </div>

          {refused ? <p className="text-xs text-destructive">{copy.startAnywayRefused}</p> : null}

          {confirming ? (
            confirmButtons
          ) : (
            <div className="grid gap-2">
              <div className="flex flex-wrap gap-2">
                <Button disabled={Boolean(busy)} onClick={() => void recheck()}>
                  {busy === 'recheck' ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                  {copy.checkAgain}
                </Button>
                <Button disabled={Boolean(busy)} onClick={() => void quit()} variant="secondary">
                  {busy === 'quit' ? <Loader2 className="animate-spin" /> : <Power />}
                  {copy.quit}
                </Button>
                <Button onClick={openLogs} variant="ghost">
                  <FileText />
                  {copy.openLogs}
                </Button>
                <Button className="ml-auto" disabled={Boolean(busy)} onClick={askStartAnyway} variant="ghost">
                  {copy.startAnyway}
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">{copy.recoveryHint}</p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
