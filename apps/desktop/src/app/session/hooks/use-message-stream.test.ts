import { describe, expect, it } from 'vitest'

import { focusSessionIdForNotification } from './use-message-stream'

describe('focusSessionIdForNotification', () => {
  it('maps a runtime session id to its stored session id for notification focus', () => {
    const sessionStateByRuntimeId = new Map([
      ['runtime-1', { storedSessionId: 'stored-1' }]
    ])

    expect(focusSessionIdForNotification('runtime-1', sessionStateByRuntimeId)).toBe('stored-1')
  })

  it('returns undefined when the runtime session has no stored session id yet', () => {
    const sessionStateByRuntimeId = new Map([
      ['runtime-1', { storedSessionId: null }]
    ])

    expect(focusSessionIdForNotification('runtime-1', sessionStateByRuntimeId)).toBeUndefined()
  })

  it('returns undefined when there is no runtime session id', () => {
    const sessionStateByRuntimeId = new Map([
      ['runtime-1', { storedSessionId: 'stored-1' }]
    ])

    expect(focusSessionIdForNotification(undefined, sessionStateByRuntimeId)).toBeUndefined()
  })
})
