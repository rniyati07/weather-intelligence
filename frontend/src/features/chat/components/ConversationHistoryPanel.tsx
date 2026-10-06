import { CalendarRange, Info, MapPin, MessageCircle, Plus, Settings } from 'lucide-react'
import { Link, NavLink, useParams } from 'react-router'

import { buildChatPath, ROUTES } from '@/constants/routes'
import { useConversationList } from '@/hooks/queries'
import { cn } from '@/lib/utils'
import { formatRelativeTime } from '@/utils/date'
import { rollingHistoryWindow } from '../active-trip'
import { BrandMark } from '@/components/common/BrandMark'

/** A conversation's own destination name, or null while it's still being
 * established — never invents a title. */
function destinationFor(tripContext: Record<string, unknown>): string | null {
  const destination = tripContext.destination
  if (destination && typeof destination === 'object') {
    const name = (destination as Record<string, unknown>).displayName
    if (typeof name === 'string' && name.length > 0) return name
  }
  return null
}

const RAIL_FOCUS =
  'focus-visible:ring-2 focus-visible:ring-rail-heading/70 focus-visible:outline-none'

/**
 * The conversation rail — server-backed (`GET /conversations`), never a
 * `localStorage` list (master prompt, "Conversation Persistence").
 *
 * One component, two mounting contexts: a permanent column on `lg`+ and the
 * content of a slide-in sheet below it — `ChatShell` decides which. It paints
 * its own navy background so both contexts look the same.
 */
export function ConversationHistoryPanel() {
  const { data, isPending, isError } = useConversationList()
  const { conversationId } = useParams<{ conversationId?: string }>()
  const conversations = data ? rollingHistoryWindow(data, conversationId) : data

  return (
    <nav
      aria-label="Conversation history"
      className="flex h-full flex-col bg-rail text-rail-foreground"
    >
      <div className="px-5 pt-5 pb-6">
        <Link
          to={ROUTES.home}
          className={cn('inline-flex min-h-11 items-center rounded-md', RAIL_FOCUS)}
        >
          <BrandMark tone="dark" />
        </Link>
      </div>

      <div className="px-4">
        <Link
          to={ROUTES.home}
          className={cn(
            'flex h-11 items-center gap-2.5 rounded-[10px] bg-primary px-4 text-body font-medium text-primary-foreground transition-colors hover:bg-primary-hover',
            RAIL_FOCUS,
          )}
        >
          <Plus className="size-4" aria-hidden="true" />
          New trip
        </Link>
      </div>

      <h2 className="mt-7 px-5 text-body-sm font-medium text-rail-muted">Recent trips</h2>

      <div className="mt-2 flex-1 scroll-thin overflow-y-auto px-3 pb-3">
        {isPending ? (
          <ul className="flex flex-col gap-1" aria-label="Loading conversations">
            {[0, 1, 2].map((key) => (
              <li key={key} className="flex flex-col gap-2 rounded-[10px] px-3 py-3">
                <span className="h-3 w-3/4 animate-pulse rounded-full bg-rail-active" />
                <span className="h-2.5 w-1/2 animate-pulse rounded-full bg-rail-hover" />
              </li>
            ))}
          </ul>
        ) : isError ? (
          <p className="px-3 py-2 text-body-sm text-rail-muted">
            Couldn’t load your conversations. They’ll appear here once the service responds.
          </p>
        ) : !conversations || conversations.length === 0 ? (
          <p className="px-3 py-2 text-body-sm text-rail-muted">
            No trips yet. Start one by telling me where you’re going.
          </p>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {conversations.map((conversation) => {
              const destination = destinationFor(conversation.tripContext)
              const Icon = destination ? MapPin : MessageCircle
              return (
                <li key={conversation.id}>
                  <NavLink
                    to={buildChatPath(conversation.id)}
                    className={({ isActive }) =>
                      cn(
                        'relative flex gap-3 rounded-[10px] px-3 py-2.5 transition-colors',
                        RAIL_FOCUS,
                        isActive ? 'bg-rail-active' : 'hover:bg-rail-hover',
                      )
                    }
                  >
                    {({ isActive }) => (
                      <>
                        {isActive ? (
                          <span
                            className="absolute top-2.5 bottom-2.5 left-0 w-[3px] rounded-full bg-primary"
                            aria-hidden="true"
                          />
                        ) : null}
                        <Icon
                          className={cn(
                            'mt-0.5 size-4 shrink-0',
                            isActive ? 'text-rail-heading' : 'text-rail-muted',
                          )}
                          aria-hidden="true"
                        />
                        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                          <span className="flex items-baseline gap-2">
                            <span
                              className={cn(
                                'min-w-0 flex-1 truncate text-body-sm font-medium',
                                isActive ? 'text-rail-heading' : 'text-rail-foreground',
                              )}
                            >
                              {destination ?? 'Untitled trip'}
                            </span>
                            <span className="shrink-0 text-caption text-rail-muted">
                              {formatRelativeTime(conversation.updatedAt)}
                            </span>
                          </span>
                          {conversation.lastMessagePreview ? (
                            <span className="truncate text-caption text-rail-muted">
                              {conversation.lastMessagePreview}
                            </span>
                          ) : null}
                        </span>
                      </>
                    )}
                  </NavLink>
                </li>
              )
            })}
          </ul>
        )}
      </div>

      <div className="flex flex-col gap-0.5 border-t border-rail-border px-3 py-3">
        <RailLink to={ROUTES.plan} icon={CalendarRange}>
          Plan with a form
        </RailLink>
        <RailLink to={ROUTES.settings} icon={Settings}>
          Settings
        </RailLink>
        <RailLink to={ROUTES.about} icon={Info}>
          How it works
        </RailLink>
      </div>
    </nav>
  )
}

function RailLink({
  to,
  icon: Icon,
  children,
}: {
  to: string
  icon: typeof Settings
  children: React.ReactNode
}) {
  return (
    <Link
      to={to}
      className={cn(
        'flex h-9 items-center gap-2.5 rounded-md px-3 text-body-sm text-rail-muted transition-colors hover:bg-rail-hover hover:text-rail-heading',
        RAIL_FOCUS,
      )}
    >
      <Icon className="size-4 shrink-0" aria-hidden="true" />
      {children}
    </Link>
  )
}
