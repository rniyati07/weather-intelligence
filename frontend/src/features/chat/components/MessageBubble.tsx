import type { ChatMessage } from '@/types'
import { getMessageContextComplete, getMessageLlmGenerated, getMessagePlaces } from '@/utils/chat'
import { renderMessageText } from '@/utils/markdown'
import { AssistantMark } from '@/components/common/BrandMark'

export interface MessageBubbleProps {
  message: ChatMessage
}

/**
 * One conversation turn.
 *
 * Renders `message.content` and nothing else — no parsing, no fact
 * extraction, no derived labels. Every structured element that accompanies a
 * reply (trip outlook, day strip, places) is a sibling in the workspace,
 * never derived from this component.
 *
 * The one exception, and it isn't really one: `renderMessageText` is handed
 * this turn's own `places[]` (`getMessagePlaces`) so it can turn a place name
 * that appears in the prose or a table cell into a link to that same real
 * place's Maps location. That's cross-referencing already-fetched structured
 * data against text the model already wrote — the same grounding the
 * sidebar's `PlaceList` already relies on — never inventing a fact, a name,
 * or a URL that wasn't already real.
 *
 * The assistant turn is deliberately *not* a tinted, bordered container: a
 * reply is the page's primary reading, and wrapping every one in a coloured
 * card made long answers overwhelming and turned the transcript into a stack
 * of boxes. The AI cue is carried by the small avatar and the byline instead,
 * which is enough to tell generated prose from computed panels at a glance.
 * The user turn keeps a compact bubble, since it needs to read as an aside.
 */
export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user'

  if (isUser) {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-[18px] rounded-br-md bg-heading px-4 py-2.5 text-body text-background md:max-w-[70%]">
          {renderMessageText(message.content)}
        </div>
      </div>
    )
  }

  // `llmGenerated: false` alone also describes every deterministic
  // clarifying question ("Where would you like to go?") — those are
  // working as designed, not degraded summaries. The label belongs only on
  // a full trip answer that had to fall back to structured data.
  const isFallbackSummary =
    getMessageLlmGenerated(message) === false && getMessageContextComplete(message) === true
  const places = getMessagePlaces(message)

  return (
    <div className="flex items-start gap-3.5">
      <AssistantMark className="mt-0.5" />

      <div className="min-w-0 flex-1">
        <p className="flex items-center gap-2 text-body-sm">
          <span className="font-semibold text-heading">Weather Intelligence</span>
          {/* Only rendered when the field says so, never inferred from the
              text (master prompt, "LLM Generated State"). Quiet, not an
              error — chat degrades to a deterministic summary server-side
              rather than failing (FDS Revision 2 §13.3). */}
          {isFallbackSummary ? (
            <span className="rounded-full bg-muted px-2 py-0.5 text-caption text-muted-foreground">
              Structured summary
            </span>
          ) : null}
        </p>

        <div className="mt-1.5 max-w-[68ch] text-body-lg text-foreground [&_p+p]:mt-3.5 [&_strong]:font-semibold [&_strong]:text-heading">
          {renderMessageText(message.content, places)}
        </div>
      </div>
    </div>
  )
}
