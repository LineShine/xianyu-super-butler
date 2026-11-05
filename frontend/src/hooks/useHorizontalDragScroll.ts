import { useEffect } from 'react'

const DRAG_THRESHOLD = 6
const EXCLUDED = 'input, textarea, select, [contenteditable]:not([contenteditable="false"]), [draggable="true"], [role="slider"], [role="scrollbar"], [data-drag-scroll="ignore"]'

/** Find the innermost actual horizontal scroller, including dynamically rendered tables/modals. */
function horizontalScroller(target: Element): HTMLElement | null {
  for (let node: Element | null = target; node && node !== document.body; node = node.parentElement) {
    if (!(node instanceof HTMLElement) || node.clientWidth === 0 || node.scrollWidth <= node.clientWidth + 1) continue
    if (/^(auto|scroll)$/.test(getComputedStyle(node).overflowX)) return node
  }
  return null
}

/** One delegated mouse gesture handler for all horizontal lists; touch keeps its native behavior. */
export function useHorizontalDragScroll() {
  useEffect(() => {
    let gesture: {
      container: HTMLElement
      pointerId: number
      x: number
      y: number
      left: number
      top: number
      dragging: boolean
    } | null = null
    let suppressClickUntil = 0

    const finish = () => {
      const previous = gesture
      gesture = null
      document.documentElement.classList.remove('horizontal-mouse-dragging')
      if (!previous) return
      if (previous.dragging) suppressClickUntil = performance.now() + 500
      if (previous.container.hasPointerCapture(previous.pointerId)) {
        previous.container.releasePointerCapture(previous.pointerId)
      }
    }

    const onDown = (event: PointerEvent) => {
      finish()
      suppressClickUntil = 0
      if (event.defaultPrevented || event.pointerType !== 'mouse' || event.button !== 0 || !event.isPrimary
        || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
      const target = event.target
      if (!(target instanceof Element) || target.closest(EXCLUDED)) return
      const container = horizontalScroller(target)
      if (!container) return
      // Leave native horizontal/vertical scrollbar handles alone.
      const rect = container.getBoundingClientRect()
      if (event.clientX < rect.left + container.clientLeft || event.clientX >= rect.left + container.clientLeft + container.clientWidth
        || event.clientY < rect.top + container.clientTop || event.clientY >= rect.top + container.clientTop + container.clientHeight) return
      gesture = { container, pointerId: event.pointerId, x: event.clientX, y: event.clientY,
        left: container.scrollLeft, top: container.scrollTop, dragging: false }
    }

    const onMove = (event: PointerEvent) => {
      if (!gesture || event.pointerId !== gesture.pointerId) return
      if (!(event.buttons & 1) || !gesture.container.isConnected) {
        finish()
        return
      }
      const dx = event.clientX - gesture.x
      const dy = event.clientY - gesture.y
      if (!gesture.dragging) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) < DRAG_THRESHOLD) return
        // Commit to the first direction. A vertical gesture cannot later become horizontal.
        if (Math.abs(dy) >= Math.abs(dx)) {
          finish()
          return
        }
        gesture.dragging = true
        document.documentElement.classList.add('horizontal-mouse-dragging')
        window.getSelection()?.removeAllRanges()
        gesture.container.setPointerCapture(event.pointerId)
      }
      event.preventDefault()
      event.stopPropagation()
      gesture.container.scrollLeft = gesture.left - dx
      gesture.container.scrollTop = gesture.top
    }

    const onEnd = (event: PointerEvent) => {
      if (gesture?.pointerId === event.pointerId) finish()
    }
    const onClick = (event: MouseEvent) => {
      if (event.detail > 0 && performance.now() < suppressClickUntil) {
        event.preventDefault()
        event.stopImmediatePropagation()
        suppressClickUntil = 0
      }
    }
    const onWheel = (event: WheelEvent) => {
      if (gesture?.dragging) {
        event.preventDefault()
        event.stopPropagation()
      }
    }
    const onNativeDrag = (event: DragEvent) => {
      // Images/links should scroll their list instead of starting a browser URL drag.
      // Explicit draggable widgets were excluded in onDown.
      if (gesture) event.preventDefault()
    }
    const onSelect = (event: Event) => {
      if (gesture) event.preventDefault()
    }
    const onVisibility = () => { if (document.hidden) finish() }

    document.addEventListener('pointerdown', onDown)
    document.addEventListener('pointermove', onMove, { capture: true, passive: false })
    document.addEventListener('pointerup', onEnd, true)
    document.addEventListener('pointercancel', onEnd, true)
    document.addEventListener('lostpointercapture', onEnd, true)
    document.addEventListener('click', onClick, true)
    document.addEventListener('wheel', onWheel, { capture: true, passive: false })
    document.addEventListener('dragstart', onNativeDrag, true)
    document.addEventListener('selectstart', onSelect, true)
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('blur', finish)
    return () => {
      finish()
      document.removeEventListener('pointerdown', onDown)
      document.removeEventListener('pointermove', onMove, true)
      document.removeEventListener('pointerup', onEnd, true)
      document.removeEventListener('pointercancel', onEnd, true)
      document.removeEventListener('lostpointercapture', onEnd, true)
      document.removeEventListener('click', onClick, true)
      document.removeEventListener('wheel', onWheel, true)
      document.removeEventListener('dragstart', onNativeDrag, true)
      document.removeEventListener('selectstart', onSelect, true)
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('blur', finish)
    }
  }, [])
}
