// Error card model for a failed turn.
//
// The backend's presentation layer (trove/services/errors/present.py) emits a
// structured `error_info` — user-facing title/explanation/suggestion plus
// machine detail (raw text / node / error_class). The frontend renders THAT;
// it never parses the error markdown and never re-classifies the failure.

import type { DoneSummary, ErrorInfo } from '../api/types'
import { stepLabel } from './steps'

export interface ErrorCardModel {
  title: string
  explanation: string
  suggestion: string
  /** 权限/凭据类失败不可重试 —— 前端据此隐藏「重试」。 */
  retryable: boolean
  /** 折叠区(管理员默认可见):原文 + 归属节点 + 错误类别。 */
  detail: {
    raw: string
    node: string
    nodeLabel: string
    errorClass: string
    domain: string
  }
}

const FALLBACK = {
  zh: {
    title: '回答没有完成',
    suggestion: '可以重试一次;若反复失败,换个说法再问,或到管理端检查数据源状态。',
  },
  en: {
    title: 'The answer did not complete',
    suggestion:
      'You can retry; if it keeps failing, rephrase the question or check the datasource status in the admin console.',
  },
}

/** 老会话(本轮之前)只存了原始错误串:去掉 markdown 标题与折叠块。 */
function _plain(text: string): string {
  return text
    .replace(/^\s*\*\*\s*(错误|Error)\s*\*\*\s*[:：]?\s*/i, '')
    .replace(/<details[\s\S]*?<\/details>/gi, '')
    .trim()
}

export function errorCard(
  source: Pick<DoneSummary, 'error' | 'error_info'> | null | undefined,
  lang: string,
): ErrorCardModel | null {
  const info: ErrorInfo | undefined = source?.error_info
  if (info && (info.title || info.explanation || info.detail?.raw)) {
    const node = info.detail?.node ?? ''
    return {
      title: info.title || FALLBACK.zh.title,
      explanation: info.explanation || '',
      suggestion: info.suggestion || '',
      retryable: info.retryable !== false,
      detail: {
        raw: info.detail?.raw ?? source?.error ?? '',
        node,
        nodeLabel: node
          ? `${stepLabel(node, lang)}（${node}）`
          : stepLabel('workflow', lang),
        errorClass: info.detail?.error_class ?? '',
        domain: info.detail?.domain ?? '',
      },
    }
  }

  const raw = _plain(source?.error ?? '')
  if (!raw) return null
  const copy = lang === 'zh' ? FALLBACK.zh : FALLBACK.en
  return {
    title: copy.title,
    explanation: raw,
    suggestion: copy.suggestion,
    retryable: true,
    detail: {
      raw: source?.error ?? '',
      node: '',
      nodeLabel: '',
      errorClass: '',
      domain: '',
    },
  }
}
