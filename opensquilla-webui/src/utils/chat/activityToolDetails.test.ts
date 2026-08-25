import { describe, expect, it } from 'vitest'
import type { ChatToolCallRenderItem } from '@/types/chat'
import {
  activityDisplayPath,
  projectActivityToolDetail,
  redactActivityDetail,
} from './activityToolDetails'

function call(
  overrides: Partial<ChatToolCallRenderItem> = {},
): ChatToolCallRenderItem {
  return {
    toolId: 'tool-1',
    renderKey: 'tool-1',
    name: 'write_file',
    displayName: 'write_file',
    inputRaw: '{}',
    inputPreview: '{}',
    isRunning: false,
    status: 'success',
    isError: false,
    result: '',
    resultPreview: '',
    isOpen: false,
    ...overrides,
  }
}

describe('activity tool detail projection', () => {
  it.each(['document.read', 'document.update'])(
    'hides all protocol detail for %s activity',
    operationKey => {
      const raw = JSON.stringify({
        expectedSha256: 'a'.repeat(64),
        cursor: 'private-cursor',
        grant: 'one-time-grant',
        document_apply: true,
      })
      const projection = projectActivityToolDetail(call({
        name: operationKey === 'document.read' ? 'document_read' : 'document_apply',
        inputRaw: raw,
        inputPreview: raw,
        result: raw,
        resultPreview: raw,
      }), operationKey)

      expect(projection).toEqual({ lines: [], rawContent: '' })
    },
  )

  it('shows only allowlisted document failure guidance', () => {
    const sensitive = JSON.stringify({
      category: 'DOCUMENT_PREVIEW_UNAVAILABLE',
      message_key: 'document.previewUnavailable',
      user_message: '<html>secret source</html>',
      retry_policy: 'new_turn',
      next_action: 'finalize_without_tools',
      expectedSha256: 'a'.repeat(64),
      cursor: 'private-cursor',
      bindingToken: 'private-binding-token',
    })
    const projection = projectActivityToolDetail(call({
      name: 'document_apply',
      status: 'error',
      isError: true,
      inputRaw: sensitive,
      result: sensitive,
    }), 'document.update')

    expect(projection).toEqual({
      lines: [
        { kind: 'document-category', category: 'DOCUMENT_PREVIEW_UNAVAILABLE' },
        { kind: 'document-message', messageKey: 'document.previewUnavailable' },
        { kind: 'document-retry', policy: 'new_turn' },
        { kind: 'document-next-action', action: 'finalize_without_tools' },
      ],
      rawContent: '',
    })
    expect(JSON.stringify(projection)).not.toMatch(/sha256|cursor|binding|secret source/i)
  })

  it('maps unknown document failure fields to localized-safe generic values', () => {
    const projection = projectActivityToolDetail(call({
      name: 'document_apply',
      status: 'error',
      isError: true,
      result: JSON.stringify({
        category: 'PRIVATE_SERVER_FAILURE_WITH_TOKEN',
        message_key: 'private.rawMessage',
        user_message: 'private source and token',
      }),
    }), 'document.update')

    expect(projection).toEqual({
      lines: [
        { kind: 'document-category', category: 'DOCUMENT_EDIT_FAILED' },
        { kind: 'document-message', messageKey: 'document.editFailed' },
        { kind: 'document-retry', policy: 'never' },
        { kind: 'document-next-action', action: 'stop' },
      ],
      rawContent: '',
    })
    expect(JSON.stringify(projection)).not.toMatch(/private|token|source/i)
  })

  it('recovers a safe failure disclosure from compatibility history flags', () => {
    const projection = projectActivityToolDetail(call({
      name: 'document_browser_inspect',
      status: 'success',
      isError: false,
      result: JSON.stringify({
        ok: false,
        status: 'error',
        category: 'DOCUMENT_PREVIEW_UNAVAILABLE',
        message_key: 'document.previewUnavailable',
        retry_policy: 'new_turn',
        next_action: 'finalize_without_tools',
        source: '<html>private source</html>',
        bindingToken: 'private-binding-token',
      }),
    }), 'document.read')

    expect(projection).toEqual({
      lines: [
        { kind: 'document-category', category: 'DOCUMENT_PREVIEW_UNAVAILABLE' },
        { kind: 'document-message', messageKey: 'document.previewUnavailable' },
        { kind: 'document-retry', policy: 'new_turn' },
        { kind: 'document-next-action', action: 'finalize_without_tools' },
      ],
      rawContent: '',
    })
    expect(JSON.stringify(projection)).not.toMatch(/private|binding|source/i)
  })

  it('shows workspace-relative file details without putting raw paths in lines', () => {
    const projection = projectActivityToolDetail(call({
      inputRaw: JSON.stringify({
        path: '/private/tmp/opensquilla-test/workspace/games/index.html',
        content: '<html>private body</html>',
      }),
      inputPreview: '{"path":"…"}',
      result: 'Written 35726 bytes to /private/tmp/opensquilla-test/workspace/games/index.html',
      resultPreview: 'Written 35726 bytes to /private/tmp/opensquilla-test/workspace/games/index.html',
    }), 'file.write')

    expect(projection.lines).toEqual([
      { kind: 'target', text: 'games/index.html' },
      { kind: 'bytes', bytes: 35726 },
    ])
    expect(JSON.stringify(projection.lines)).not.toContain('/private/')
    expect(JSON.stringify(projection.lines)).not.toContain('private body')
    expect(projection.rawContent).toContain('/private/tmp/opensquilla-test')
  })

  it('uses artifact names and status instead of raw artifact payloads', () => {
    const projection = projectActivityToolDetail(call({
      name: 'publish_artifact',
      inputRaw: JSON.stringify({
        path: '/private/tmp/opensquilla-test/workspace/games/index.html',
        name: '小游戏合集.html',
      }),
      result: JSON.stringify({
        status: 'published',
        artifact: { id: 'internal-id', download_url: 'https://example.test/private' },
      }),
    }), 'artifact.create')

    expect(projection.lines).toEqual([
      { kind: 'target', text: '小游戏合集.html' },
      { kind: 'published' },
    ])
    expect(JSON.stringify(projection.lines)).not.toContain('internal-id')
  })

  it('reduces path-shaped artifact and unknown-tool names to safe targets', () => {
    expect(projectActivityToolDetail(call({
      name: 'publish_artifact',
      inputRaw: JSON.stringify({
        name: '/Users/example/private/report.html',
      }),
    }), 'artifact.create').lines).toEqual([
      { kind: 'target', text: '…/report.html' },
    ])

    expect(projectActivityToolDetail(call({
      inputRaw: JSON.stringify({
        title: 'C:\\Users\\example\\private\\report.txt',
      }),
    }), 'tool.unknown').lines).toEqual([
      { kind: 'target', text: '…/report.txt' },
    ])
  })

  it('only exposes safe HTTP locations and file basenames for URL-shaped targets', () => {
    expect(projectActivityToolDetail(call({
      inputRaw: JSON.stringify({
        url: 'https://example.test/docs/page?access_token=secret',
      }),
    }), 'web.read').lines).toEqual([
      { kind: 'target', text: 'example.test/docs/page' },
    ])

    expect(projectActivityToolDetail(call({
      inputRaw: JSON.stringify({
        url: 'file:///Users/example/private/report.txt',
      }),
    }), 'web.read').lines).toEqual([
      { kind: 'target', text: '…/report.txt' },
    ])

    expect(projectActivityToolDetail(call({
      inputRaw: JSON.stringify({
        url: 'mailto:private@example.test',
      }),
    }), 'web.read').lines).toEqual([])
    expect(projectActivityToolDetail(call({
      inputRaw: JSON.stringify({
        url: 'data:text/plain,private-payload',
      }),
    }), 'web.read').lines).toEqual([])
  })

  it('keeps raw commands behind the explicit detail viewer', () => {
    const projection = projectActivityToolDetail(call({
      name: 'shell',
      inputRaw: JSON.stringify({
        command: 'OPENAI_API_KEY=sk-secret npm test --password hidden',
      }),
    }), 'command.run')

    // No input-derived detail line at all: command lines carry credentials in
    // shapes a browser-only projection cannot classify exhaustively.
    expect(projection.lines).toEqual([])
    expect(projection.rawContent).toContain('OPENAI_API_KEY=sk-secret')
  })

  it('reports content size for read-shaped and generic operations only', () => {
    const longOutput = Array.from({ length: 42 }, (_, i) => `line ${i}`).join('\n')

    expect(projectActivityToolDetail(call({
      name: 'shell',
      result: longOutput,
      resultPreview: longOutput,
    }), 'command.run').lines).toEqual([])

    expect(projectActivityToolDetail(call({
      name: 'web_search',
      inputRaw: JSON.stringify({ query: 'vue flexbox wrap' }),
      result: longOutput,
      resultPreview: longOutput,
    }), 'web.search').lines).toEqual([
      { kind: 'target', text: '“vue flexbox wrap”' },
    ])

    expect(projectActivityToolDetail(call({
      name: 'read_file',
      inputRaw: '{"path":"src/App.vue"}',
      result: longOutput,
      resultPreview: longOutput,
    }), 'file.inspect').lines).toEqual([
      { kind: 'target', text: 'src/App.vue' },
      { kind: 'content-size', lines: 42, characters: longOutput.length },
    ])

    // Generic tools (MCP servers and custom integrations) rarely carry a
    // name-like input key, so the size line is their only signal.
    expect(projectActivityToolDetail(call({
      name: 'custom_connector_action',
      inputRaw: JSON.stringify({ payload: 'step one\nstep two' }),
      result: longOutput,
      resultPreview: longOutput,
    }), 'tool.custom.connector.action').lines).toEqual([
      { kind: 'content-size', lines: 42, characters: longOutput.length },
    ])
  })

  it('reduces absolute error paths to a basename', () => {
    const projection = projectActivityToolDetail(call({
      status: 'error',
      isError: true,
      result: 'Unable to open /Users/example/private/project/file.txt: permission denied',
      resultPreview: 'Unable to open /Users/example/private/project/file.txt: permission denied',
    }), 'file.inspect')

    expect(projection.lines).toEqual([
      { kind: 'error', text: 'Unable to open …/file.txt: permission denied' },
    ])
  })

  it('prefers a safe structured user message while retaining raw error details', () => {
    const raw = JSON.stringify({
      status: 'error',
      tool: 'image',
      error_class: 'SafeToolError',
      message: 'Internal wrapper failed',
      user_message: 'Image path is not accessible by the image tool.',
    })
    const projection = projectActivityToolDetail(call({
      status: 'error',
      isError: true,
      result: raw,
      resultPreview: '{"status":"error","tool":"image",…',
    }), 'tool.image')

    expect(projection.lines).toEqual([
      {
        kind: 'error',
        text: 'Image path is not accessible by the image tool.',
      },
    ])
    expect(projection.rawContent).toContain('"error_class":"SafeToolError"')
    expect(projection.rawContent).toContain('"message":"Internal wrapper failed"')
  })

  it('projects failed shell results as a localizable exit-code fact', () => {
    const projection = projectActivityToolDetail(call({
      name: 'shell',
      status: 'error',
      isError: true,
      result: 'exit_code=1\nstderr=synthetic failure',
      resultPreview: 'exit_code=1',
    }), 'command.run')

    expect(projection.lines).toEqual([
      { kind: 'exit-code', code: 1 },
    ])
    expect(projection.rawContent).toContain('stderr=synthetic failure')
  })

  it('keeps relative paths and hides external directory structure', () => {
    expect(activityDisplayPath('src/components/App.vue')).toBe('src/components/App.vue')
    expect(activityDisplayPath('C:\\Users\\example\\secret\\App.vue')).toBe('…/App.vue')
    expect(activityDisplayPath('/Users/example/secret/App.vue')).toBe('…/App.vue')
    expect(activityDisplayPath(
      '/tmp/workspace/../../Users/example/secret/key.txt',
    )).toBe('…/key.txt')
  })

  it('treats home-anchored paths as absolute, not workspace-relative', () => {
    expect(activityDisplayPath('~/secret/id_rsa')).toBe('…/id_rsa')
    expect(activityDisplayPath('$HOME/secret/id_rsa')).toBe('…/id_rsa')
    expect(activityDisplayPath('%USERPROFILE%\\secret\\id_rsa.pub')).toBe('…/id_rsa.pub')
    // `$HOMEWORK/...` is an ordinary relative directory, not a home anchor.
    expect(activityDisplayPath('$HOMEWORK/notes.txt')).toBe('$HOMEWORK/notes.txt')
  })

  it('only derives written-byte metadata for file mutations', () => {
    expect(projectActivityToolDetail(call({
      name: 'shell',
      result: 'Written 2048 bytes',
      resultPreview: 'Written 2048 bytes',
    }), 'command.run').lines).toEqual([])
    expect(projectActivityToolDetail(call({
      name: 'inspect_file',
      result: 'Written 2048 bytes',
      resultPreview: 'Written 2048 bytes',
    }), 'file.inspect').lines).toEqual([])
  })

  it('keeps file mutation errors visible even when output mentions written bytes', () => {
    expect(projectActivityToolDetail(call({
      status: 'error',
      isError: true,
      inputRaw: '{"path":"src/App.vue"}',
      result: 'Written 2048 bytes before verification failed',
      resultPreview: 'Written 2048 bytes before verification failed',
    }), 'file.write').lines).toEqual([
      { kind: 'target', text: 'src/App.vue' },
      { kind: 'error', text: 'Written 2048 bytes before verification failed' },
    ])
  })

  it('classifies raw input-only and mixed details without result highlighting', () => {
    expect(projectActivityToolDetail(call({
      inputRaw: '{"path":"src/App.vue"}',
    }), 'file.inspect').rawSection).toBe('input')

    expect(projectActivityToolDetail(call({
      inputRaw: '{"path":"src/App.vue"}',
      result: 'file contents',
    }), 'file.inspect').rawSection).toBeUndefined()
  })

  it('redacts sensitive structured values', () => {
    expect(redactActivityDetail(
      '{"password":"secret","apiKey":"hidden"} token=private',
    )).toBe(
      '{"password":"[redacted]","apiKey":"[redacted]"} token=[redacted]',
    )
  })

  it('redacts common environment, flag, bearer, and URL credentials', () => {
    expect(redactActivityDetail([
      'OPENAI_API_KEY=sk-environment-secret',
      '--password flag-secret',
      'Authorization: Bearer bearer-secret-value',
      'https://user:basic-secret@example.test/path?access_token=query-secret',
      'ghp_abcdefghijklmnopqrstuvwxyz',
    ].join('\n'))).toBe([
      'OPENAI_API_KEY=[redacted]',
      '--password [redacted]',
      'Authorization: Bearer [redacted]',
      'https://[redacted]@example.test/path?access_token=[redacted]',
      '[redacted]',
    ].join('\n'))
  })

  it('redacts underscore-form provider keys and bare JWTs', () => {
    expect(redactActivityDetail('sk_live_a1b2c3d4e5f6')).toBe('[redacted]')
    expect(redactActivityDetail('sk_test_a1b2c3d4e5f6')).toBe('[redacted]')
    expect(redactActivityDetail('sk_proj_a1b2c3d4e5f6')).toBe('[redacted]')
    expect(redactActivityDetail(
      'jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkdW1teSJ9.c2lnbmF0dXJlLXBhcnQ',
    )).toBe('jwt [redacted]')
  })
})
