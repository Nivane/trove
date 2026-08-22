// Centralized user-facing notices. Routes errors from API layers and stores
// into Element Plus messages so nothing fails silently.

import { ElMessage } from 'element-plus'

export function notifySuccess(msg: string) {
  ElMessage({ type: 'success', message: msg, duration: 2500 })
}

export function notifyError(msg: string) {
  ElMessage({ type: 'error', message: msg, duration: 4000 })
}

export function notifyInfo(msg: string) {
  ElMessage({ type: 'info', message: msg, duration: 2500 })
}

/** Humanize an unknown thrown value into a message and surface it. */
export function toastError(err: unknown, fallback = 'Something went wrong') {
  const msg =
    err && typeof err === 'object' && 'message' in err
      ? String((err as { message: unknown }).message)
      : String(err ?? '')
  notifyError(msg || fallback)
}
