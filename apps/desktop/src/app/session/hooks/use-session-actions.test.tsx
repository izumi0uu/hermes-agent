import { act, cleanup, render } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { assistantTextPart, chatMessageText, type ChatMessage } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import { setSessions } from '@/store/session'
import type { SessionMessagesResponse, UsageStats } from '@/types/hermes'

import { useSessionActions } from './use-session-actions'

const mocks = vi.hoisted(() => ({
  ensureGatewayProfile: vi.fn(async () => undefined),
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  getSessionMessages: vi.fn<(...args: unknown[]) => Promise<SessionMessagesResponse>>(),
  setApiRequestProfile: vi.fn()
}))

vi.mock('@/hermes', async importOriginal => {
  const actual = await importOriginal<typeof import('@/hermes')>()

  return {
    ...actual,
    getProfiles: mocks.getProfiles,
    getSessionMessages: mocks.getSessionMessages,
    setApiRequestProfile: mocks.setApiRequestProfile
  }
})

vi.mock('@/store/profile', async importOriginal => {
  const actual = await importOriginal<typeof import('@/store/profile')>()

  return {
    ...actual,
    ensureGatewayProfile: mocks.ensureGatewayProfile
  }
})

interface HarnessHandle {
  resumeSession: (storedSessionId: string, replaceRoute?: boolean) => Promise<void>
}

function assistantMessage(id: string, text: string): ChatMessage {
  return {
    id,
    parts: [assistantTextPart(text)],
    role: 'assistant'
  }
}

function cachedState(): ClientSessionState {
  return {
    ...createClientSessionState('stored-session-1', [assistantMessage('cached-assistant', 'stale window copy')]),
    storedSessionId: 'stored-session-1'
  }
}

function requestGatewayMock(usage: UsageStats) {
  const impl = async <T,>(method: string) => {
    if (method === 'session.usage') {
      return usage as T
    }

    return {} as T
  }

  return vi.fn(impl) as unknown as <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}

function Harness({
  onReady,
  onSync,
  requestGateway
}: {
  onReady: (handle: HarnessHandle) => void
  onSync: (sessionId: string, state: ClientSessionState) => void
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}) {
  const activeSessionIdRef = useRef<string | null>('runtime-session-1')
  const busyRef = useRef(false)
  const creatingSessionRef = useRef(false)
  const runtimeIdByStoredSessionIdRef = useRef(new Map([['stored-session-1', 'runtime-session-1']]))
  const selectedStoredSessionIdRef = useRef<string | null>('stored-session-1')
  const sessionStateByRuntimeIdRef = useRef(new Map([['runtime-session-1', cachedState()]]))

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef,
    busyRef,
    creatingSessionRef,
    ensureSessionState: sessionId => sessionStateByRuntimeIdRef.current.get(sessionId) ?? cachedState(),
    getRouteToken: () => '/stored-session-1',
    navigate: vi.fn(),
    requestGateway,
    runtimeIdByStoredSessionIdRef,
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef,
    sessionStateByRuntimeIdRef,
    syncSessionStateToView: (sessionId, state) => {
      onSync(sessionId, state)
    },
    updateSessionState: (sessionId, updater) => {
      const current = sessionStateByRuntimeIdRef.current.get(sessionId) ?? cachedState()
      const next = updater(current)
      sessionStateByRuntimeIdRef.current.set(sessionId, next)
      onSync(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    onReady({ resumeSession: actions.resumeSession })
  }, [actions.resumeSession, onReady])

  return null
}

describe('useSessionActions resumeSession', () => {
  beforeEach(() => {
    setSessions(() => [
      {
        ended_at: null,
        id: 'stored-session-1',
        input_tokens: 0,
        is_active: true,
        last_active: 0,
        message_count: 2,
        model: null,
        output_tokens: 0,
        preview: 'fresh from window A',
        profile: 'builder',
        source: 'cli',
        started_at: 0,
        title: 'Session 1',
        tool_call_count: 0
      }
    ])
    mocks.ensureGatewayProfile.mockClear()
    mocks.getProfiles.mockClear()
    mocks.getSessionMessages.mockReset()
    mocks.setApiRequestProfile.mockClear()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('rehydrates a cached session from stored messages so another window sees the latest transcript', async () => {
    mocks.getSessionMessages.mockResolvedValue({
      messages: [{ content: 'fresh from window A', role: 'assistant', timestamp: 1 }],
      session_id: 'stored-session-1'
    })

    const requestGateway = requestGatewayMock({ calls: 1, input: 2, output: 3, total: 5 })
    const syncs: ClientSessionState[] = []
    let handle: HarnessHandle | null = null

    render(
      <Harness
        onReady={next => {
          handle = next
        }}
        onSync={(_sessionId, state) => {
          syncs.push(state)
        }}
        requestGateway={requestGateway}
      />
    )

    await act(async () => {
      await handle!.resumeSession('stored-session-1')
    })

    expect(mocks.ensureGatewayProfile).toHaveBeenCalledWith('builder')
    expect(mocks.getSessionMessages).toHaveBeenCalledWith('stored-session-1', 'builder')
    expect(syncs).toHaveLength(2)
    expect(chatMessageText(syncs.at(-1)!.messages[0]!)).toBe('fresh from window A')
    expect(requestGateway).toHaveBeenCalledWith('session.usage', { session_id: 'runtime-session-1' })
  })

  it('keeps the warm cache visible when the stored rehydrate fails', async () => {
    mocks.getSessionMessages.mockRejectedValue(new Error('state.db busy'))

    const requestGateway = requestGatewayMock({ calls: 0, input: 0, output: 0, total: 0 })
    const syncs: ClientSessionState[] = []
    let handle: HarnessHandle | null = null

    render(
      <Harness
        onReady={next => {
          handle = next
        }}
        onSync={(_sessionId, state) => {
          syncs.push(state)
        }}
        requestGateway={requestGateway}
      />
    )

    await act(async () => {
      await handle!.resumeSession('stored-session-1')
    })

    expect(mocks.getSessionMessages).toHaveBeenCalledWith('stored-session-1', 'builder')
    expect(syncs).toHaveLength(1)
    expect(chatMessageText(syncs[0]!.messages[0]!)).toBe('stale window copy')
  })
})
