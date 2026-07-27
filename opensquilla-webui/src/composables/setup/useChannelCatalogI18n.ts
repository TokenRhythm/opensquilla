import { useI18n } from 'vue-i18n'

// The backend channel catalog ships label/description/whatYouNeed in English.
// This overlay localizes them by (type, field) key, falling back to the
// backend English whenever a key is absent — so a channel type or field the
// overlay has not translated yet degrades to English instead of breaking.
// Keys live under `setup.channelCatalog.<type>` and are covered for every
// catalog type by scripts/check-channel-catalog-i18n.mjs.
export function useChannelCatalogI18n() {
  const { t, te, tm } = useI18n()

  function tr(key: string, fallback: string): string {
    return te(key) ? t(key) : fallback
  }

  function localizeDescription(type: string, fallback?: string): string {
    return tr(`setup.channelCatalog.${type}.description`, fallback || '')
  }

  // Platform display name: localized when the overlay carries one (the CN
  // platforms read as 飞书/企业微信/钉钉/QQ 机器人 in zh), else the backend label.
  function localizeLabel(type: string, fallback: string): string {
    return tr(`setup.channelCatalog.${type}.label`, fallback)
  }

  function localizeNeeds(type: string, fallback?: string[]): string[] {
    // `te` reports false for array messages, so probe the resolved value:
    // tm returns the localized array when present, else a non-array to fall back.
    const localized = tm(`setup.channelCatalog.${type}.needs`)
    if (Array.isArray(localized) && localized.length > 0 && localized.every(x => typeof x === 'string')) {
      return localized as string[]
    }
    return fallback || []
  }

  // Field labels/descriptions: a per-type key wins, else the shared key
  // (common fields like name/agent_id/enabled appear in every channel type),
  // else the backend English fallback.
  function localizeFieldLabel(type: string, name: string, fallback: string): string {
    const typed = `setup.channelCatalog.${type}.fields.${name}.label`
    if (te(typed)) return t(typed)
    return tr(`setup.channelCatalog.fields.${name}.label`, fallback)
  }

  function localizeFieldDescription(type: string, name: string, fallback: string): string {
    const typed = `setup.channelCatalog.${type}.fields.${name}.description`
    if (te(typed)) return t(typed)
    return tr(`setup.channelCatalog.fields.${name}.description`, fallback)
  }

  // Group headers are shared vocabulary across channel types (credentials,
  // webhook, …): a per-type key wins when present, else the shared key, else
  // the caller's fallback (the humanized backend group name).
  function localizeGroupLabel(type: string, group: string, fallback: string): string {
    const typed = `setup.channelCatalog.${type}.groups.${group}`
    if (te(typed)) return t(typed)
    return tr(`setup.channelCatalog.groups.${group}`, fallback)
  }

  // Select-choice display labels: the backend ships raw enum values
  // ("feishu", "lark"); an overlay key names them for humans (飞书（中国）
  // / Lark（国际）). Per-type key wins, then the shared key, else the raw
  // choice value unchanged — the submitted value is always the raw choice.
  function localizeFieldChoice(type: string, name: string, choice: string, fallback: string): string {
    const typed = `setup.channelCatalog.${type}.fields.${name}.choices.${choice}`
    if (te(typed)) return t(typed)
    return tr(`setup.channelCatalog.fields.${name}.choices.${choice}`, fallback)
  }

  return {
    localizeDescription,
    localizeLabel,
    localizeNeeds,
    localizeFieldLabel,
    localizeFieldChoice,
    localizeFieldDescription,
    localizeGroupLabel,
  }
}
