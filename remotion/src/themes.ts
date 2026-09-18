export interface Theme {
  name: string;
  background: string;
  fontFamily: string;
  caption: {
    color: string;
    activeColor: string;
    emphasisColor: string;
    fontSize: number;
    fontWeight: number;
    strokeColor: string;
    strokeWidth: number;
    shadow: string;
    /** Distance from the bottom edge (px) so captions clear the Shorts UI. */
    bottomOffset: number;
    maxWidth: number;
    uppercase: boolean;
  };
  overlay: {
    color: string;
    background: string;
    fontSize: number;
    fontWeight: number;
    topOffset: number;
    borderRadius: number;
  };
  progress: {
    color: string;
    trackColor: string;
    height: number;
  };
  /** Subtle push-in applied to video segments (0 disables). */
  videoDriftScale: number;
  /** Ken Burns intensity for still images. */
  kenBurnsScale: number;
  /** Safe zones (px) that no text may enter. */
  safeTop: number;
  safeBottom: number;
  safeSide: number;
}

const base: Theme = {
  name: 'default',
  background: '#000000',
  fontFamily:
    'Inter, "Helvetica Neue", Helvetica, Arial, "Segoe UI", Roboto, system-ui, sans-serif',
  caption: {
    color: '#FFFFFF',
    activeColor: '#FFD60A',
    emphasisColor: '#FF6B35',
    fontSize: 72,
    fontWeight: 800,
    strokeColor: 'rgba(0,0,0,0.9)',
    strokeWidth: 10,
    shadow: '0 6px 24px rgba(0,0,0,0.65)',
    bottomOffset: 520,
    maxWidth: 940,
    uppercase: true,
  },
  overlay: {
    color: '#FFFFFF',
    background: 'rgba(0,0,0,0.55)',
    fontSize: 84,
    fontWeight: 900,
    topOffset: 300,
    borderRadius: 24,
  },
  progress: {color: '#FFD60A', trackColor: 'rgba(255,255,255,0.18)', height: 10},
  videoDriftScale: 0.05,
  kenBurnsScale: 0.12,
  safeTop: 220,
  safeBottom: 420,
  safeSide: 60,
};

export const THEMES: Record<string, Theme> = {
  default: base,
  clean: {
    ...base,
    name: 'clean',
    caption: {...base.caption, activeColor: '#FFFFFF', emphasisColor: '#4CC9F0', uppercase: false, fontWeight: 700, strokeWidth: 8},
    overlay: {...base.overlay, background: 'rgba(255,255,255,0.92)', color: '#111111'},
    progress: {...base.progress, color: '#4CC9F0'},
    videoDriftScale: 0.03,
  },
  bold: {
    ...base,
    name: 'bold',
    caption: {...base.caption, fontSize: 82, activeColor: '#00FF88', emphasisColor: '#FF3366', strokeWidth: 12},
    overlay: {...base.overlay, background: '#FF3366', fontSize: 96},
    progress: {...base.progress, color: '#00FF88'},
    videoDriftScale: 0.07,
  },
};

export const getTheme = (name: string | undefined): Theme => THEMES[name ?? 'default'] ?? base;
