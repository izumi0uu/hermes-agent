import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useTerminalSession } from './use-terminal-session'

const testState = vi.hoisted(() => {
  const fitSizes: Array<{ cols: number; rows: number }> = []

  const resizeObservers = new Set<{
    target: Element | null
    trigger: () => void
  }>()

  class MockTerminal {
    cols = 80
    rows = 24
    options: Record<string, unknown>
    unicode = { activeVersion: '' }

    constructor(options: Record<string, unknown>) {
      this.options = options
    }

    clearSelection() {}

    dispose() {}

    focus() {}

    getSelection() {
      return ''
    }

    getSelectionPosition() {
      return null
    }

    hasSelection() {
      return false
    }

    loadAddon(addon: { __terminal?: MockTerminal }) {
      addon.__terminal = this
    }

    onData(_cb: (data: string) => void) {
      return { dispose() {} }
    }

    onSelectionChange(_cb: () => void) {
      return { dispose() {} }
    }

    open(_host: HTMLElement) {}

    write(_data: string) {}
  }

  class MockFitAddon {
    __terminal?: MockTerminal

    fit() {
      const next = fitSizes.shift()

      if (!next || !this.__terminal) {
        return
      }

      this.__terminal.cols = next.cols
      this.__terminal.rows = next.rows
    }
  }

  class MockUnicode11Addon {}

  class MockWebLinksAddon {}

  class MockWebglAddon {
    clearTextureAtlas() {}

    dispose() {}

    onContextLoss(_cb: () => void) {}
  }

  return {
    MockFitAddon,
    MockTerminal,
    MockUnicode11Addon,
    MockWebLinksAddon,
    MockWebglAddon,
    fitSizes,
    resizeObservers
  }
})

vi.mock('@/lib/haptics', () => ({ triggerHaptic: () => undefined }))
vi.mock('@/themes/context', () => ({
  useTheme: () => ({
    renderedMode: 'light',
    theme: {},
    themeName: 'test-skin'
  })
}))
vi.mock('./buffer', () => ({
  makeTerminalReader: () => () => null,
  setActiveTerminalReader: () => undefined
}))
vi.mock('@xterm/addon-fit', () => ({ FitAddon: testState.MockFitAddon }))
vi.mock('@xterm/addon-unicode11', () => ({ Unicode11Addon: testState.MockUnicode11Addon }))
vi.mock('@xterm/addon-web-links', () => ({ WebLinksAddon: testState.MockWebLinksAddon }))
vi.mock('@xterm/addon-webgl', () => ({ WebglAddon: testState.MockWebglAddon }))
vi.mock('@xterm/xterm', () => ({ Terminal: testState.MockTerminal }))

class TestResizeObserver {
  target: Element | null = null

  constructor(private readonly callback: ResizeObserverCallback) {
    testState.resizeObservers.add(this)
  }

  disconnect() {
    testState.resizeObservers.delete(this)
  }

  observe(target: Element) {
    this.target = target
  }

  trigger() {
    if (!this.target) {
      return
    }

    this.callback(
      [
        {
          contentRect: { height: 320, width: 640 } as DOMRectReadOnly,
          target: this.target
        } as ResizeObserverEntry
      ],
      this as unknown as ResizeObserver
    )
  }
}

function Harness() {
  const { hostRef } = useTerminalSession({ cwd: '/repo', onAddSelectionToChat: () => undefined })

  return <div data-testid="host" ref={hostRef} />
}

describe('useTerminalSession', () => {
  const dataListeners = new Map<string, (data: string) => void>()
  const exitListeners = new Map<string, (payload: { code: null | number; signal: null | string }) => void>()
  const start = vi.fn<() => Promise<{ id: string; shell: string }>>()
  const resize = vi.fn<(id: string, size: { cols: number; rows: number }) => Promise<void>>()
  const write = vi.fn<(id: string, data: string) => Promise<void>>()
  const dispose = vi.fn<(id: string) => Promise<void>>()
  let clientHeightDescriptor: PropertyDescriptor | undefined
  let clientWidthDescriptor: PropertyDescriptor | undefined

  beforeEach(() => {
    vi.useFakeTimers()
    vi.stubGlobal('ResizeObserver', TestResizeObserver)
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(performance.now()), 0)
    )
    vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
    clientWidthDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth')
    clientHeightDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight')
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
      configurable: true,
      get() {
        return 640
      }
    })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
      configurable: true,
      get() {
        return 320
      }
    })

    testState.fitSizes.length = 0
    testState.resizeObservers.clear()
    dataListeners.clear()
    exitListeners.clear()
    start.mockReset()
    resize.mockReset()
    write.mockReset()
    dispose.mockReset()
    start.mockResolvedValue({ id: 'session-1', shell: 'zsh' })
    resize.mockResolvedValue(undefined)
    write.mockResolvedValue(undefined)
    dispose.mockResolvedValue(undefined)

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        terminal: {
          dispose,
          onData: (id: string, cb: (data: string) => void) => {
            dataListeners.set(id, cb)

            return () => {
              dataListeners.delete(id)
            }
          },
          onExit: (id: string, cb: (payload: { code: null | number; signal: null | string }) => void) => {
            exitListeners.set(id, cb)

            return () => {
              exitListeners.delete(id)
            }
          },
          resize,
          start,
          write
        }
      }
    })
  })

  afterEach(() => {
    cleanup()
    vi.runOnlyPendingTimers()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    Reflect.deleteProperty(window, 'hermesDesktop')

    if (clientWidthDescriptor) {
      Object.defineProperty(HTMLElement.prototype, 'clientWidth', clientWidthDescriptor)
    }

    if (clientHeightDescriptor) {
      Object.defineProperty(HTMLElement.prototype, 'clientHeight', clientHeightDescriptor)
    }
  })

  it('sends only one pristine Ctrl-L redraw across repeated launch-time resizes', async () => {
    testState.fitSizes.push(
      { cols: 80, rows: 24 },
      { cols: 81, rows: 24 },
      { cols: 82, rows: 24 },
      { cols: 83, rows: 24 },
      { cols: 84, rows: 24 }
    )

    render(<Harness />)

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
      await vi.advanceTimersByTimeAsync(0)
      await Promise.resolve()
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(start).toHaveBeenCalledWith({ cols: 80, cwd: '/repo', rows: 24 })
    expect(dataListeners.has('session-1')).toBe(true)

    act(() => {
      dataListeners.get('session-1')?.('\r\n\r\nprompt $ ')
    })

    for (let i = 0; i < 3; i += 1) {
      await act(async () => {
        for (const observer of [...testState.resizeObservers]) {
          observer.trigger()
        }

        await vi.advanceTimersByTimeAsync(0)
      })

      await act(async () => {
        await vi.advanceTimersByTimeAsync(130)
      })
    }

    expect(resize).toHaveBeenCalledTimes(4)
    expect(write.mock.calls.filter(([, data]) => data === '\f')).toHaveLength(1)
  })
})
