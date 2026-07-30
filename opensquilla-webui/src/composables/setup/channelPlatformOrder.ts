// Locale-aware platform ordering, shared by the compose gallery and the
// /channels platform bar so the two add-entry surfaces can never drift out of
// order. zh locales lead with the CN-ecosystem platforms; every other locale
// leads with Slack/Telegram/Discord/Matrix — the most likely pick sits first.
export const ZH_PLATFORM_ORDER = ['feishu', 'wecom', 'dingtalk', 'qq', 'slack', 'telegram', 'discord', 'matrix']
export const DEFAULT_PLATFORM_ORDER = ['slack', 'telegram', 'discord', 'matrix', 'feishu', 'wecom', 'dingtalk', 'qq']

export function platformOrderFor(locale: string): string[] {
  return String(locale).toLowerCase().startsWith('zh') ? ZH_PLATFORM_ORDER : DEFAULT_PLATFORM_ORDER
}

// Stable, locale-ranked sort: known types by their tier position, unknown types
// last, ties broken by the localized display label so the order is deterministic.
export function orderChannelSpecs<T extends { type: string }>(
  specs: T[],
  locale: string,
  label: (spec: T) => string,
): T[] {
  const order = platformOrderFor(locale)
  const rank = new Map(order.map((type, index) => [type, index]))
  return [...specs].sort((a, b) => {
    const rankA = rank.get(a.type) ?? order.length
    const rankB = rank.get(b.type) ?? order.length
    return rankA - rankB || label(a).localeCompare(label(b))
  })
}
