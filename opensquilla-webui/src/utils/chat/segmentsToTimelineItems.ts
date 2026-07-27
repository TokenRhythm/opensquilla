import type {
  ChatStreamSegment,
  ChatStreamTimelineItem,
  ChatToolCall,
  ChatToolCallGroup,
} from '@/types/chat'
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
      }]
    }
    const group = seg.groupId ? groupsById.get(seg.groupId) : null
    return group ? [{ type: 'tool-group', key: seg.groupId || `tool-${idx}`, group }] : []
  })
}
