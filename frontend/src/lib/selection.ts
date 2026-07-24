// Границы выделенного пользователем текста внутри элемента — в символах
// исходной строки, а не в узлах DOM. Нужно, чтобы «вырезать выделенное»
// резало ровно то, что видно на экране.

export interface Offsets {
  start: number;
  end: number;
  text: string;
}

/**
 * Смещения текущего выделения относительно начала текста элемента.
 * null — выделения нет, оно схлопнуто или лежит вне элемента.
 *
 * Работает при условии, что элемент рендерит текст сообщения как есть
 * (одним текстовым узлом, без вставок разметки) — см. ячейку .msg-text.
 */
export function selectionOffsets(el: HTMLElement | null): Offsets | null {
  if (!el) return null;
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;

  const range = sel.getRangeAt(0);
  if (!el.contains(range.commonAncestorContainer)) return null;

  const before = range.cloneRange();
  before.selectNodeContents(el);
  before.setEnd(range.startContainer, range.startOffset);
  const start = before.toString().length;
  const text = range.toString();
  if (!text.trim()) return null;

  return { start, end: start + text.length, text };
}
