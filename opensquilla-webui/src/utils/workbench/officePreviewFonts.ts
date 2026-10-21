import sansRegularUrl from '@office-preview-fonts/NotoSansSC-Regular.otf?url'
import sansBoldUrl from '@office-preview-fonts/NotoSansSC-Bold.otf?url'
import serifRegularUrl from '@office-preview-fonts/NotoSerifSC-Regular.otf?url'
import serifBoldUrl from '@office-preview-fonts/NotoSerifSC-Bold.otf?url'

let loading: Promise<void> | undefined

/** Load the same offline fallback faces before Silurus measures or paints text.
 * These are fallback families, not aliases for authored Microsoft fonts.
 * Parser workers do not measure text; render-worker mode needs its own loader.
 */
export function ensureOfficePreviewFonts(): Promise<void> {
  if (loading) return loading
  loading = (async () => {
    const definitions = [
      ['Noto Sans SC', '400', sansRegularUrl],
      ['Noto Sans SC', '700', sansBoldUrl],
      ['Noto Serif SC', '400', serifRegularUrl],
      ['Noto Serif SC', '700', serifBoldUrl],
    ] as const
    const faces = await Promise.all(definitions.map(async ([family, weight, url]) => {
      const face = new FontFace(family, `url(${JSON.stringify(url)})`, { weight, style: 'normal', display: 'block' })
      await face.load()
      return face
    }))
    for (const face of faces) document.fonts.add(face)
    await Promise.all(definitions.map(([family, weight]) => document.fonts.load(`${weight} 16px "${family}"`, '中文预览宋体黑体')))
  })().catch((error: unknown) => {
    loading = undefined
    throw error
  })
  return loading
}
