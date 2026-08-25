import type {
  ChatStreamSegment,
  ChatStreamTimelineItem,
  ChatToolCall,
  ChatToolCallGroup,
} from '@/types/chat'
import type { ChatPart } from '@/types/parts'
import { toolCallGroups } from '@/utils/chat/toolDisplay'

/**
 * Pure flatMap of ordered stream segments + their tool calls into render
 * timeline items. Extracted verbatim from the live `streamTimelineItems`
 * computed so the legacy live path and the live-turn fold share one ordering
 * implementation and cannot diverge on item shape, key, or group recovery.
 */
export function segmentsToTimelineItems(
  segments: ChatStreamSegment[],
  toolCalls: ChatToolCall[],
  baseKey: string,
  interruptParts: ReadonlyMap<
    string,
    Extract<ChatPart, { type: 'interrupt' }>
  > = new Map(),
): ChatStreamTimelineItem[] {
  const groupsById = new Map<string, ChatToolCallGroup>(
    toolCallGroups(toolCalls, baseKey).map(group => [group.groupId, group]),
  )
  return segments.flatMap((seg, idx): ChatStreamTimelineItem[] => {
    if (seg.type === 'text') {
      if (!seg.raw && !seg.html) return []
      return [{
        type: 'text',
        key: `text-${idx}`,
        html: seg.html || '',
        rawText: seg.raw || '',
        presentation: seg.presentation,
        activityOrder: seg.activityOrder,
      }]
    }
    if (seg.type === 'interrupt') {
      const approvalId = String(seg.approvalId || '')
      const part = approvalId ? interruptParts.get(approvalId) : null
      return part
        ? [{
            type: 'interrupt',
            key: part.key,
            approvalId,
            part,
            activityOrder: seg.activityOrder,
          }]
        : []
    }
    const group = seg.groupId ? groupsById.get(seg.groupId) : null
    if (!group) return []
    const activityOrder = seg.activityOrder
      ?? group.calls.reduce<number | undefined>((minimum, call) => (
        call.activityOrder === undefined
          ? minimum
          : minimum === undefined
            ? call.activityOrder
            : Math.min(minimum, call.activityOrder)
      ), undefined)
    return [{
      type: 'tool-group',
      key: seg.groupId || `tool-${idx}`,
      group: {
        ...group,
        ...(activityOrder !== undefined ? { activityOrder } : {}),
      },
      ...(activityOrder !== undefined ? { activityOrder } : {}),
    }]
  })
}
