import { create } from "zustand";

export type PanelTab = "memory" | "tvs" | "workshop";

export interface Appearance {
  reducedMotion: boolean;
  scanlines: boolean;
  fontScale: number;
}

const KEY = "ultron.appearance";
const DEFAULTS: Appearance = { reducedMotion: false, scanlines: false, fontScale: 1 };

function load(): Appearance {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw) return { ...DEFAULTS, ...(JSON.parse(raw) as Partial<Appearance>) };
  } catch {
    /* storage can be blocked; fall back to defaults */
  }
  return DEFAULTS;
}

function save(value: Appearance): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}

interface UiState {
  drawerOpen: boolean;
  sheetOpen: boolean;
  panelTab: PanelTab;
  drafts: Record<string, string>;
  appearance: Appearance;
  setDrawer: (open: boolean) => void;
  setSheet: (open: boolean) => void;
  setPanelTab: (tab: PanelTab) => void;
  setDraft: (chatId: string, text: string) => void;
  setAppearance: (patch: Partial<Appearance>) => void;
}

export const useUi = create<UiState>((set, get) => ({
  drawerOpen: false,
  sheetOpen: false,
  panelTab: "memory",
  drafts: {},
  appearance: load(),
  setDrawer: (drawerOpen) => set({ drawerOpen }),
  setSheet: (sheetOpen) => set({ sheetOpen }),
  setPanelTab: (panelTab) => set({ panelTab, sheetOpen: true }),
  setDraft: (chatId, text) => set((s) => ({ drafts: { ...s.drafts, [chatId]: text } })),
  setAppearance: (patch) => {
    const appearance = { ...get().appearance, ...patch };
    save(appearance);
    set({ appearance });
  },
}));

/** Applies appearance choices to the document root. */
export function applyAppearance(a: Appearance): void {
  const root = document.documentElement;
  root.dataset.motion = a.reducedMotion ? "reduced" : "full";
  root.style.setProperty("--font-scale", String(a.fontScale));
  document.body.classList.toggle("scan", a.scanlines);
}
