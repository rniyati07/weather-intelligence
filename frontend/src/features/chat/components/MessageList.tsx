import { useEffect, useRef } from 'react'

import type { AnyErrorCode, ChatMessage } from '@/types'
import { deriveFollowUpSuggestions, getMessagePlaces } from '@/utils/chat'
import { ChatErrorBanner } from './ChatErrorBanner'
import { EmptyConversation } from './EmptyConversation'
import { FollowUpSuggestions } from './FollowUpSuggestions'
import { MessageBubble } from './MessageBubble'
import { PlaceList } from './PlaceList'
import { TypingIndicator } from './TypingIndicator'

export interface MessageListProps {
  messages: ChatMessage[]
  isSending: boolean
  errorCode: AnyErrorCode | undefined
  onRetry: () => void
  onSelectSuggestion: (text: string) => void
  onPickExample: (text: string) => void
  /** Rendered between the identity header and the transcript — the structured
   * workspace, which belongs to the trip rather than to any one turn. */
  workspace?: React.ReactNode
}

/**
 * The transcript. `role="log"` + `aria-live="polite"` so a new assistant
 * message is announced without stealing focus from the composer.
 *
 * Structured content is composed *around* messages, never inside
 * `MessageBubble`. Trip-level intelligence (outlook, days) mounts once, above
 * the stream, because it describes the trip rather than a turn. Per-turn
 * places stay attached to the turn that returned them — but only when that
 * turn is the latest, so the transcript doesn't accumulate repeated lists as
 * the conversation grows.
 */
export function MessageList({
  messages,
  isSending,
  errorCode,
  onRetry,
  onSelectSuggestion,
  onPickExample,
  workspace,
}: MessageListProps) {
  const endRef = useRef<HTMLDivElement>(null)

  // Follow the conversation only as it grows. Opening a conversation keeps
  // the view at the top, where the trip outlook is — the most important
  // thing on the page — rather than jumping past it to the last message.
  const seenLength = useRef(messages.length)
  useEffect(() => {
    if (messages.length > seenLength.current || isSending) {
      endRef.current?.scrollIntoView({ block: 'end' })
    }
    seenLength.current = messages.length
  }, [messages.length, isSending])

  if (messages.length === 0 && !isSending) {
    return <EmptyConversation onPick={onPickExample} />
  }

  return (
    <div className="flex min-w-0 flex-1 scroll-thin flex-col overflow-y-auto">
      <div className="mx-auto flex w-full max-w-[58rem] min-w-0 flex-col gap-7 px-4 pt-6 pb-10 md:px-10 md:pt-8">
        {workspace}

        {workspace ? (
          <h2 className="flex items-center gap-3 pt-2 text-body-sm font-medium text-muted-foreground">
            Conversation
            <span className="h-px flex-1 bg-border" aria-hidden="true" />
          </h2>
        ) : null}

        <div
          role="log"
          aria-live="polite"
          aria-relevant="additions"
          className="flex min-w-0 flex-col gap-7"
        >
          {messages.map((message, index) => {
            const isLast = index === messages.length - 1
            const places = isLast && message.role === 'assistant' ? getMessagePlaces(message) : []

            return (
              <div key={message.id} className="flex flex-col gap-4">
                <MessageBubble message={message} />

                {places.length > 0 ? (
                  <div className="rounded-lg bg-surface px-4 py-1 shadow-xs md:ml-[42px]">
                    <PlaceList places={places} />
                  </div>
                ) : null}

                {isLast && !isSending && message.role === 'assistant' ? (
                  <div className="md:ml-[42px]">
                    <FollowUpSuggestions
                      suggestions={deriveFollowUpSuggestions(message)}
                      onSelect={onSelectSuggestion}
                    />
                  </div>
                ) : null}
              </div>
            )
          })}

          {isSending ? <TypingIndicator /> : null}

          {!isSending && errorCode ? <ChatErrorBanner code={errorCode} onRetry={onRetry} /> : null}
        </div>

        <div ref={endRef} />
      </div>
    </div>
  )
}
