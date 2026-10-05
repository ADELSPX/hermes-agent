import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopUpdateHold } from '@/global'
import { en } from '@/i18n/en'
import { $desktopBoot, applyDesktopUpdateHold } from '@/store/boot'

import { UpdateHoldOverlay } from './update-hold-overlay'

// The blocked-update surface (R8 D3/M6). A hold met by the primary boot is the
// full-screen blocked boot screen; a hold only a background pool/profile
// backend meets while Hermes already runs is a non-blocking banner (R9-6).

const copy = en.boot.updateHold

function hold(scope?: DesktopUpdateHold['scope']): DesktopUpdateHold {
  return {
    holdId: 'a1b2c3d4e5f60718',
    verdict: 'held',
    ownerPid: 4242,
    since: Date.now() - 6_000,
    checkedAt: Date.now(),
    logPath: '/x/logs/update.log',
    ...(scope ? { scope } : {})
  }
}

let actions: {
  recheck: ReturnType<typeof vi.fn>
  quit: ReturnType<typeof vi.fn>
  startAnyway: ReturnType<typeof vi.fn>
}

const original = window.hermesDesktop

beforeEach(() => {
  actions = {
    recheck: vi.fn(async () => ({ ok: true })),
    quit: vi.fn(async () => ({ ok: true })),
    startAnyway: vi.fn(async () => ({ ok: true }))
  }
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { updateHold: actions, revealLogs: vi.fn(async () => undefined) }
  })
  $desktopBoot.set({
    error: null,
    fakeMode: false,
    message: 'Hermes is ready',
    phase: 'backend.ready',
    progress: 100,
    running: false,
    timestamp: Date.now(),
    visible: false
  })
})

afterEach(() => {
  cleanup()
  applyDesktopUpdateHold(null)
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
})

describe('UpdateHoldOverlay', () => {
  it('a startup hold keeps the full-screen blocked boot screen', () => {
    applyDesktopUpdateHold(hold('startup'))
    render(<UpdateHoldOverlay />)

    const dialog = screen.getByRole('alertdialog')
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(screen.getByTestId('update-hold-overlay')).toBeTruthy()
    expect(screen.getByText(copy.title)).toBeTruthy()
    expect(screen.getByRole('button', { name: copy.quit })).toBeTruthy()
    expect(screen.queryByTestId('update-hold-banner')).toBeNull()
  })

  it('a hold without a scope (older main) is still the startup screen', () => {
    applyDesktopUpdateHold(hold())
    render(<UpdateHoldOverlay />)

    expect(screen.getByRole('alertdialog')).toBeTruthy()
  })

  it('a background pool/profile hold is a non-blocking banner, not a mask over the running app (R9-6)', async () => {
    applyDesktopUpdateHold(hold('background'))
    render(<UpdateHoldOverlay />)

    expect(screen.queryByRole('alertdialog'), 'never the full-screen modal while Hermes runs').toBeNull()
    expect(screen.queryByTestId('update-hold-overlay')).toBeNull()

    const banner = screen.getByTestId('update-hold-banner')
    expect(banner.getAttribute('aria-modal')).toBeNull()
    expect(banner.className).not.toContain('inset-0')
    expect(screen.getByText(copy.backgroundTitle)).toBeTruthy()
    expect(screen.getByText(copy.backgroundDescription)).toBeTruthy()
    expect(screen.queryByText(copy.description), 'no startup wording').toBeNull()
    expect(screen.queryByRole('button', { name: copy.quit }), 'quitting the whole app is not its way out').toBeNull()

    fireEvent.click(screen.getByRole('button', { name: copy.checkAgain }))
    await waitFor(() => expect(actions.recheck).toHaveBeenCalledTimes(1))
    // Main answers the re-check with a fresh `checkedAt` on the same hold.
    act(() => applyDesktopUpdateHold({ ...hold('background'), checkedAt: Date.now() + 1_000 }))

    fireEvent.click(screen.getByRole('button', { name: copy.startAnyway }))
    expect(screen.getByText(copy.backgroundConfirmTitle)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: copy.confirmStart }))
    await waitFor(() =>
      expect(actions.startAnyway).toHaveBeenCalledWith({ holdId: 'a1b2c3d4e5f60718', confirmed: true })
    )
  })

  it('a refused background Start anyway says so and stays on the banner', async () => {
    actions.startAnyway.mockResolvedValueOnce({ ok: false })
    applyDesktopUpdateHold(hold('background'))
    render(<UpdateHoldOverlay />)

    fireEvent.click(screen.getByRole('button', { name: copy.startAnyway }))
    fireEvent.click(screen.getByRole('button', { name: copy.confirmStart }))
    await waitFor(() => expect(screen.getByText(copy.startAnywayRefused)).toBeTruthy())
    expect(screen.getByTestId('update-hold-banner')).toBeTruthy()
  })
})
