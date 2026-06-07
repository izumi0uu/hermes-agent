import { act, cleanup, render } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import type { NavigateFunction } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import type * as HermesApi from '@/hermes'
import type { ChatMessage } from '@/lib/chat-messages'
import { assistantTextPart, chatMessageText, toChatMessages } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import type * as ProfileStore from '@/store/profile'
import { $forkOriginNotices, setSessions } from '@/store/session'
import type { SessionInfo, SessionMessagesResponse, SessionResumeResponse, UsageStats } from '@/types/hermes'

import { sessionRoute } from '../../routes'

import { useSessionActions } from './use-session-actions'

const mocks = vi.hoisted(() => ({
  ensureGatewayProfile: vi.fn(async () => undefined),
    forkSession: vi.fn(),
    getProfiles: vi.fn(async () => ({ profiles: [] })),
    getSessionMessages: vi.fn<(...args: unknown[]) => Promise<SessionMessagesResponse>>(),
    setApiRequestProfile: vi.fn()
  }))

vi.mock('@/hermes', async importOriginal => {
  const actual = await importOriginal<typeof HermesApi>()

  return {
    ...actual,
    forkSession: mocks.forkSession,
    getProfiles: mocks.getProfiles,
    getSessionMessages: mocks.getSessionMessages,
    setApiRequestProfile: mocks.setApiRequestProfile
  }
})

vi.mock('@/store/profile', async importOriginal => {
  const actual = await importOriginal<typeof ProfileStore>()

  return {
    ...actual,
    ensureGatewayProfile: mocks.ensureGatewayProfile
  }
})

interface HarnessHandle {
  branchCurrentSession: (messageId?: string) => Promise<boolean>
  resumeSession: (storedSessionId: string, replaceRoute?: boolean) => Promise<void>
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void

  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })

  return { promise, reject, resolve }
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
  const impl = async <T,>(method: string, params?: Record<string, unknown>) => {
    if (method === 'session.resume') {
      return {
        info: { cwd: '/repo/fork', model: 'gpt-5.4' },
        message_count: 2,
        messages: [
          { id: 101, content: 'forked prompt', role: 'user', timestamp: 11 },
          { id: 102, content: 'forked answer', role: 'assistant', timestamp: 12 }
        ],
        resumed: String(params?.session_id ?? ''),
        session_id: 'runtime-session-2'
      } as T
    }

    if (method === 'session.usage') {
      return usage as T
    }

    return {} as T
  }

  return vi.fn(impl) as unknown as <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}

function Harness({
  initialActiveSessionId = 'runtime-session-1',
  initialRuntimeIds = [['stored-session-1', 'runtime-session-1']] as Array<[string, string]>,
  initialSessionStates = [['runtime-session-1', cachedState()]] as Array<[string, ClientSessionState]>,
  onReady,
  onSync,
  navigate,
  requestGateway
}: {
  initialActiveSessionId?: string | null
  initialRuntimeIds?: Array<[string, string]>
  initialSessionStates?: Array<[string, ClientSessionState]>
  onReady: (handle: HarnessHandle) => void
  onSync: (sessionId: string, state: ClientSessionState) => void
  navigate: NavigateFunction
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}) {
  const activeSessionIdRef = useRef<string | null>(initialActiveSessionId)
  const busyRef = useRef(false)
  const creatingSessionRef = useRef(false)
  const runtimeIdByStoredSessionIdRef = useRef(new Map(initialRuntimeIds))
  const selectedStoredSessionIdRef = useRef<string | null>('stored-session-1')
  const sessionStateByRuntimeIdRef = useRef(new Map(initialSessionStates))

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef,
    busyRef,
    creatingSessionRef,
    ensureSessionState: sessionId => sessionStateByRuntimeIdRef.current.get(sessionId) ?? cachedState(),
    getRouteToken: () => '/stored-session-1',
    navigate,
    requestGateway,
    runtimeIdByStoredSessionIdRef,
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef,
    sessionStateByRuntimeIdRef,
    syncSessionStateToView: (sessionId, state) => {
      onSync(sessionId, state)
    },
    updateSessionState: (sessionId, updater) => {
      const current =
        sessionStateByRuntimeIdRef.current.get(sessionId) ?? createClientSessionState(selectedStoredSessionIdRef.current)

      const next = updater(current)
      sessionStateByRuntimeIdRef.current.set(sessionId, next)
      onSync(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    onReady({ branchCurrentSession: actions.branchCurrentSession, resumeSession: actions.resumeSession })
  }, [actions.branchCurrentSession, actions.resumeSession, onReady])

  return null
}

describe('useSessionActions resumeSession', () => {
  beforeEach(() => {
    $forkOriginNotices.set({})
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
    mocks.forkSession.mockReset()
    mocks.getProfiles.mockClear()
    mocks.getSessionMessages.mockReset()
    mocks.setApiRequestProfile.mockClear()
  })

  afterEach(() => {
    cleanup()
    $forkOriginNotices.set({})
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
        navigate={vi.fn() as unknown as NavigateFunction}
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
        navigate={vi.fn() as unknown as NavigateFunction}
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

  it('forks from the stored transcript instead of creating a synthetic session', async () => {
    const sourceTranscript: SessionMessagesResponse = {
      messages: [
        { id: 1, content: 'check this repo', role: 'user', timestamp: 1 },
        {
          id: 2,
          content: 'Let me inspect that.',
          role: 'assistant',
          timestamp: 2,
          tool_calls: [{ id: 'tc-1', function: { name: 'search_files', arguments: '{"path":"src"}' } }]
        },
        {
          id: 3,
          content: '{"output":"src/app.ts"}',
          role: 'tool',
          tool_call_id: 'tc-1',
          tool_name: 'search_files',
          timestamp: 3
        },
        { id: 4, content: 'Found the entrypoint.', role: 'assistant', timestamp: 4 }
      ],
      session_id: 'stored-session-1'
    }

    const forkedTranscript: SessionMessagesResponse = {
      messages: [
        { id: 11, content: 'check this repo', role: 'user', timestamp: 11 },
        { id: 12, content: 'Found the entrypoint.', role: 'assistant', timestamp: 12 }
      ],
      session_id: 'forked-session-1'
    }

    const forkedSession: SessionInfo = {
      cwd: '/repo/fork',
      ended_at: null,
      id: 'forked-session-1',
      input_tokens: 0,
      is_active: true,
      last_active: 20,
      message_count: 4,
      model: 'gpt-5.4',
      output_tokens: 0,
      preview: 'Found the entrypoint.',
      profile: 'builder',
      source: 'cli',
      started_at: 20,
      title: 'Session 1 #2',
      tool_call_count: 1
    }

    mocks.getSessionMessages.mockImplementation(async sessionId => {
      if (sessionId === 'stored-session-1') {
        return sourceTranscript
      }

      if (sessionId === 'forked-session-1') {
        return forkedTranscript
      }

      throw new Error(`unexpected session: ${String(sessionId)}`)
    })
    mocks.forkSession.mockResolvedValue(forkedSession)

    const requestGateway = requestGatewayMock({ calls: 2, input: 3, output: 5, total: 8 })
    const navigate = vi.fn() as unknown as NavigateFunction
    const syncs: ClientSessionState[] = []
    let handle: HarnessHandle | null = null

    render(
      <Harness
        navigate={navigate}
        onReady={next => {
          handle = next
        }}
        onSync={(_sessionId, state) => {
          syncs.push(state)
        }}
        requestGateway={requestGateway}
      />
    )

    const assistantMessageId = toChatMessages(sourceTranscript.messages).find(message => message.role === 'assistant')!.id

    await act(async () => {
      expect(await handle!.branchCurrentSession(assistantMessageId)).toBe(true)
    })

    expect(mocks.ensureGatewayProfile).toHaveBeenCalledWith('builder')
    expect(mocks.getSessionMessages).toHaveBeenCalledWith('stored-session-1', 'builder')
    expect(mocks.forkSession).toHaveBeenCalledWith('stored-session-1', { until_message_id: 4 }, 'builder')
    expect(navigate).toHaveBeenCalledWith(sessionRoute('forked-session-1'))
    expect(requestGateway).not.toHaveBeenCalledWith('session.create', expect.anything())
    expect(requestGateway).toHaveBeenCalledWith('session.resume', {
      cols: 96,
      profile: 'builder',
      session_id: 'forked-session-1'
    })
    expect($forkOriginNotices.get()['forked-session-1']).toMatchObject({
      branchMessageOrdinal: 1,
      parentSessionId: 'stored-session-1'
    })
    expect(chatMessageText(syncs.at(-1)!.messages.at(-1)!)).toBe('Found the entrypoint.')
  })

  it('waits for an in-flight startup resume before branching the routed session', async () => {
    const sourceTranscript: SessionMessagesResponse = {
      messages: [
        { id: 1, content: 'check this repo', role: 'user', timestamp: 1 },
        {
          id: 2,
          content: 'Let me inspect that.',
          role: 'assistant',
          timestamp: 2,
          tool_calls: [{ id: 'tc-1', function: { name: 'search_files', arguments: '{"path":"src"}' } }]
        },
        {
          id: 3,
          content: '{"output":"src/app.ts"}',
          role: 'tool',
          tool_call_id: 'tc-1',
          tool_name: 'search_files',
          timestamp: 3
        },
        { id: 4, content: 'Found the entrypoint.', role: 'assistant', timestamp: 4 }
      ],
      session_id: 'stored-session-1'
    }

    const forkedTranscript: SessionMessagesResponse = {
      messages: [
        { id: 11, content: 'check this repo', role: 'user', timestamp: 11 },
        { id: 12, content: 'Found the entrypoint.', role: 'assistant', timestamp: 12 }
      ],
      session_id: 'forked-session-1'
    }

    const forkedSession: SessionInfo = {
      cwd: '/repo/fork',
      ended_at: null,
      id: 'forked-session-1',
      input_tokens: 0,
      is_active: true,
      last_active: 20,
      message_count: 4,
      model: 'gpt-5.4',
      output_tokens: 0,
      preview: 'Found the entrypoint.',
      profile: 'builder',
      source: 'cli',
      started_at: 20,
      title: 'Session 1 #2',
      tool_call_count: 1
    }

    mocks.getSessionMessages.mockImplementation(async sessionId => {
      if (sessionId === 'stored-session-1') {
        return sourceTranscript
      }

      if (sessionId === 'forked-session-1') {
        return forkedTranscript
      }

      throw new Error(`unexpected session: ${String(sessionId)}`)
    })
    mocks.forkSession.mockResolvedValue(forkedSession)

    const sourceResume = deferred<SessionResumeResponse>()

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume' && params?.session_id === 'stored-session-1') {
        return sourceResume.promise as Promise<never>
      }

      if (method === 'session.resume' && params?.session_id === 'forked-session-1') {
        return {
          info: { cwd: '/repo/fork', model: 'gpt-5.4', running: false },
          message_count: forkedTranscript.messages.length,
          messages: forkedTranscript.messages,
          resumed: 'forked-session-1',
          session_id: 'runtime-session-2'
        } as never
      }

      if (method === 'session.usage') {
        return { calls: 2, input: 3, output: 5, total: 8 } as never
      }

      return {} as never
    })

    const navigate = vi.fn() as unknown as NavigateFunction
    let handle: HarnessHandle | null = null

    render(
      <Harness
        initialActiveSessionId={null}
        initialRuntimeIds={[]}
        initialSessionStates={[]}
        navigate={navigate}
        onReady={next => {
          handle = next
        }}
        onSync={() => undefined}
        requestGateway={requestGateway}
      />
    )

    const assistantMessageId = toChatMessages(sourceTranscript.messages).find(message => message.role === 'assistant')!.id

    act(() => {
      void handle!.resumeSession('stored-session-1')
    })

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })

    let branchSettled = false

    const branchPromise = handle!.branchCurrentSession(assistantMessageId).then(result => {
      branchSettled = result

      return result
    })

    await act(async () => {
      await Promise.resolve()
    })

    expect(branchSettled).toBe(false)
    expect(mocks.forkSession).not.toHaveBeenCalled()

    sourceResume.resolve({
      info: { cwd: '/repo/source', model: 'gpt-5.4', running: false },
      message_count: sourceTranscript.messages.length,
      messages: sourceTranscript.messages,
      resumed: 'stored-session-1',
      session_id: 'runtime-session-1'
    })

    await act(async () => {
      expect(await branchPromise).toBe(true)
    })

    expect(mocks.forkSession).toHaveBeenCalledWith('stored-session-1', { until_message_id: 4 }, 'builder')
    expect(navigate).toHaveBeenCalledWith(sessionRoute('forked-session-1'))
  })

  it('keeps branching blocked when the resumed session is still running', async () => {
    const sourceTranscript: SessionMessagesResponse = {
      messages: [{ id: 1, content: 'still running prompt', role: 'user', timestamp: 1 }],
      session_id: 'stored-session-1'
    }

    mocks.getSessionMessages.mockResolvedValue(sourceTranscript)

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume' && params?.session_id === 'stored-session-1') {
        return {
          info: { cwd: '/repo/source', model: 'gpt-5.4', running: true },
          message_count: sourceTranscript.messages.length,
          messages: sourceTranscript.messages,
          resumed: 'stored-session-1',
          session_id: 'runtime-session-1'
        } as never
      }

      return {} as never
    })

    let handle: HarnessHandle | null = null

    render(
      <Harness
        initialActiveSessionId={null}
        initialRuntimeIds={[]}
        initialSessionStates={[]}
        navigate={vi.fn() as unknown as NavigateFunction}
        onReady={next => {
          handle = next
        }}
        onSync={() => undefined}
        requestGateway={requestGateway}
      />
    )

    await act(async () => {
      await handle!.resumeSession('stored-session-1')
    })

    await act(async () => {
      expect(await handle!.branchCurrentSession()).toBe(false)
    })

    expect(mocks.forkSession).not.toHaveBeenCalled()
  })
})
