// DOM actions shared by the screens.
//
// `css`: dynamic styles (column widths, map positions, decoration widths) set through CSSOM, never
// an inline `style` attribute, which `style-src 'self'` blocks silently (BR-212).
// `trap`: a dialog keeps the focus inside it and closes on Escape (BR-232).

export type Styles = Record<string, string>;

/** Bring the ordinary control into view when reached from the setup guide. */
export function focusInView(el: HTMLElement | null | undefined): void {
  if (!el) return;
  el.focus();
  el.scrollIntoView({ block: 'center', inline: 'nearest' });
}

function applyStyles(el: HTMLElement, styles: Styles, previous: Styles): void {
  for (const name of Object.keys(previous)) {
    if (!(name in styles)) el.style.removeProperty(name);
  }
  for (const [name, value] of Object.entries(styles)) el.style.setProperty(name, value);
}

export function css(el: HTMLElement, styles: Styles): { update: (next: Styles) => void } {
  let current = styles;
  applyStyles(el, current, {});
  return {
    update(next: Styles) {
      applyStyles(el, next, current);
      current = next;
    },
  };
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// The open dialogs, last opened last: only the topmost one takes keys whose focus left it.
const open: HTMLElement[] = [];

export function trap(el: HTMLElement, onEscape: () => void): { destroy: () => void } {
  const before = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const items = (): HTMLElement[] =>
    [...el.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((e) => e.offsetParent !== null);
  const first = el.querySelector<HTMLElement>('[data-autofocus]:not([disabled])') ?? items()[0];
  queueMicrotask(() => (first ?? el).focus());
  const onKey = (event: KeyboardEvent): void => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onEscape();
      return;
    }
    if (event.key !== 'Tab') return;
    const list = items();
    if (list.length === 0) return;
    const head = list[0];
    const tail = list[list.length - 1];
    if (event.shiftKey && document.activeElement === head) {
      event.preventDefault();
      tail?.focus();
    } else if (!event.shiftKey && document.activeElement === tail) {
      event.preventDefault();
      head?.focus();
    }
  };
  // A control disabled under the focus (a stale undo's Confirm) drops it to the body: the
  // topmost dialog still closes on Escape, and Tab brings the focus back inside it.
  const outside = (event: KeyboardEvent): void => {
    if (open[open.length - 1] !== el || el.contains(event.target as Node)) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      onEscape();
    } else if (event.key === 'Tab') {
      const list = items();
      event.preventDefault();
      (event.shiftKey ? list[list.length - 1] : list[0])?.focus();
    }
  };
  open.push(el);
  el.addEventListener('keydown', onKey);
  el.ownerDocument.addEventListener('keydown', outside);
  return {
    destroy() {
      el.removeEventListener('keydown', onKey);
      el.ownerDocument.removeEventListener('keydown', outside);
      open.splice(open.indexOf(el), 1);
      if (before && before.isConnected) before.focus();
    },
  };
}

/** Copies a server value to the clipboard; false when the browser refuses (the text stays shown). */
export async function copy(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** True when the key event comes from a field, where the screen's shortcuts do not apply. */
export function typing(event: KeyboardEvent): boolean {
  const target = event.target;
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))
  );
}

/** Saves a Blob as a file through a temporary object URL. */
export function save(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
