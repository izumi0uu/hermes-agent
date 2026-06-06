// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

import { SessionActionsMenu } from './session-actions-menu'

vi.mock('@/hermes', () => ({
  renameSession: vi.fn()
}))

vi.mock('@/lib/session-export', () => ({
  exportSession: vi.fn()
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/store/session', () => ({
  setSessions: vi.fn()
}))

vi.mock('@/lib/haptics', () => ({
  triggerHaptic: vi.fn()
}))

function installDesktopBridge(partial: Partial<Window['hermesDesktop']> = {}) {
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      openWindow: vi.fn().mockResolvedValue({ ok: true }),
      ...partial
    } as Window['hermesDesktop']
  })
}

function renderMenu() {
  return render(
    <MemoryRouter>
      <I18nProvider configClient={null}>
        <SessionActionsMenu
          onArchive={vi.fn()}
          onDelete={vi.fn()}
          onPin={vi.fn()}
          pinned={false}
          sessionId="session-123"
          title="Demo Session"
        >
          <button type="button">Open menu</button>
        </SessionActionsMenu>
      </I18nProvider>
    </MemoryRouter>
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
})

describe('SessionActionsMenu', () => {
  it('opens the session route in a new desktop window', () => {
    const openWindow = vi.fn().mockResolvedValue({ ok: true })
    installDesktopBridge({ openWindow })

    renderMenu()

    fireEvent.pointerDown(screen.getByRole('button', { name: 'Open menu' }), { button: 0, ctrlKey: false })
    fireEvent.click(screen.getByRole('menuitem', { name: /Open in New Window/i }))

    expect(openWindow).toHaveBeenCalledWith({ route: '/session-123' })
  })
})
