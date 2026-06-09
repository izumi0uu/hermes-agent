import { type FC } from 'react'

import { Checkbox } from '@/components/ui/checkbox'
import { Loader2Icon } from '@/lib/icons'
import { parseTodos, type TodoItem, type TodoStatus } from '@/lib/todos'
import { cn } from '@/lib/utils'

type TodoDisplayState = 'cancelled' | 'completed' | 'incomplete' | 'running'

interface TodoPresentation {
  ariaLabel: string
  checked: boolean
  displayState: TodoDisplayState
}

export function todosFromMessageContent(content: unknown): TodoItem[] {
  if (!Array.isArray(content)) {
    return []
  }

  let latest: null | TodoItem[] = null

  for (const part of content) {
    if (!part || typeof part !== 'object') {
      continue
    }

    const row = part as Record<string, unknown>

    if (row.type !== 'tool-call' || row.toolName !== 'todo') {
      continue
    }

    const parsed = parseTodos(row.result) ?? parseTodos(row.args)

    if (parsed !== null) {
      latest = parsed
    }
  }

  return latest ?? []
}

function displayState(status: TodoStatus, active: boolean): TodoDisplayState {
  if (status === 'completed') {
    return 'completed'
  }

  if (status === 'cancelled') {
    return 'cancelled'
  }

  if (status === 'in_progress' && active) {
    return 'running'
  }

  return 'incomplete'
}

function headerPriority(state: TodoDisplayState): number {
  switch (state) {
    case 'running':
      return 0
    case 'incomplete':
      return 1
    case 'completed':
      return 2
    case 'cancelled':
      return 3
  }
}

function rowOpacityClass(state: TodoDisplayState): string {
  switch (state) {
    case 'running':
      return 'opacity-100'
    case 'incomplete':
      return 'opacity-70'
    case 'completed':
    case 'cancelled':
      return 'opacity-45'
  }
}

function presentTodo(status: TodoStatus, label: string, active: boolean): TodoPresentation {
  const state = displayState(status, active)

  if (state === 'running') {
    return {
      ariaLabel: `In progress: ${label}`,
      checked: false,
      displayState: state
    }
  }

  if (state === 'cancelled') {
    return {
      ariaLabel: `Cancelled: ${label}`,
      checked: false,
      displayState: state
    }
  }

  if (state === 'completed') {
    return {
      ariaLabel: label,
      checked: true,
      displayState: state
    }
  }

  return {
    ariaLabel: `Incomplete: ${label}`,
    checked: false,
    displayState: state
  }
}

function headerLabel(todos: readonly TodoItem[], active: boolean): string {
  const primary = todos.reduce<{ item: TodoItem; priority: number } | null>((best, todo) => {
    const priority = headerPriority(displayState(todo.status, active))

    if (!best || priority < best.priority) {
      return { item: todo, priority }
    }

    return best
  }, null)

  return primary?.item.content ?? todos.at(-1)?.content ?? 'Tasks'
}

const Checkmark: FC<{ presentation: TodoPresentation }> = ({ presentation }) => {
  if (presentation.displayState === 'running') {
    return (
      <span
        aria-label={presentation.ariaLabel}
        className="grid size-[1.1rem] shrink-0 place-items-center rounded-full border border-ring/65 bg-[color-mix(in_srgb,var(--dt-ring)_14%,transparent)]"
      >
        <Loader2Icon className="size-3 animate-spin text-ring" />
      </span>
    )
  }

  return (
    <Checkbox
      aria-label={presentation.ariaLabel}
      checked={presentation.checked}
      className={cn(
        'size-[1.1rem] shrink-0 rounded-full border-border/80 pointer-events-none disabled:cursor-default disabled:opacity-100',
        presentation.checked &&
          'data-[state=checked]:border-primary data-[state=checked]:bg-primary data-[state=checked]:text-primary-foreground [&_[data-slot=checkbox-indicator]_svg]:size-3',
        presentation.displayState === 'cancelled' && 'border-muted-foreground/40'
      )}
      disabled
    />
  )
}

export const HoistedTodoPanel: FC<{ active?: boolean; todos: TodoItem[] }> = ({ active = false, todos }) => {
  if (!todos.length) {
    return null
  }

  const label = headerLabel(todos, active)

  return (
    <section
      className="mt-1 mb-3 inline-block w-fit max-w-full overflow-hidden rounded-2xl border border-border/70 bg-card align-top shadow-[0_1px_2px_0_hsl(var(--foreground)/0.04),0_1px_4px_-1px_hsl(var(--foreground)/0.06)]"
      data-slot="aui_todo-hoisted"
    >
      <header className="px-3 pt-3 pb-2">
        <span
          className="block max-w-full truncate text-[0.85rem] font-semibold leading-tight tracking-tight text-foreground"
          title={label}
        >
          {label}
        </span>
      </header>
      <ul className="grid min-w-0 gap-0.5 px-3 pb-3">
        {todos.map(todo => {
          const presentation = presentTodo(todo.status, todo.content, active)

          return (
            <li
              className={cn(
                'flex min-w-0 items-center gap-3 py-1.5 transition-opacity',
                rowOpacityClass(presentation.displayState)
              )}
              key={todo.id}
            >
              <Checkmark presentation={presentation} />
              <span className="min-w-0 wrap-anywhere text-[0.8rem] leading-[1.2rem] text-foreground">
                {todo.content}
              </span>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
