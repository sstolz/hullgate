// Theme switch: dark by default (#18). The choice is a per-viewer convenience kept in
// localStorage, which may be absent or throw: the console works without it.

export type Theme = 'dark' | 'light';
const KEY = 'hullgate.theme';

export function stored(storage: Pick<Storage, 'getItem'> | undefined): Theme {
  try {
    return storage?.getItem(KEY) === 'light' ? 'light' : 'dark';
  } catch {
    return 'dark';
  }
}

export function store(storage: Pick<Storage, 'setItem'> | undefined, theme: Theme): void {
  try {
    storage?.setItem(KEY, theme);
  } catch {
    // private window or blocked storage: the theme lasts for this page only
  }
}

export function apply(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}
