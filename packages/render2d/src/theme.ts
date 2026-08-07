/**
 * Colour themes for the 2D board renderer.
 *
 * A theme separates two kinds of colour:
 *  - "Physical" colours (substrate, pad, hole, silk, conductor kinds) represent real
 *    material appearance and are intentionally similar across themes — a green FR-4
 *    board looks green whether the app chrome is light or dark.
 *  - "UI" colours (canvasBackground outside the board, selection, risk marker) exist
 *    only in the app and are free to adapt for contrast against the surrounding UI.
 */

import type { BoardMaterial, ConductorKind } from '@perfstudio/core';

export interface Theme {
  /** Fills the area outside the board outline (the canvas backdrop). */
  readonly canvasBackground: string;
  /** Bare substrate colour, keyed by board material. */
  readonly substrate: Readonly<Record<BoardMaterial, string>>;
  /** Copper pad annulus. */
  readonly pad: string;
  /** Drilled hole (the void through pad + substrate). */
  readonly hole: string;
  /** Silkscreen ink — also used for the component ref label. */
  readonly silk: string;
  /** Base/default colour per conductor kind, used when a conductor has no explicit colour. */
  readonly conductor: Readonly<Record<ConductorKind, string>>;
  /** Darker spine line drawn over a 'solder-trace-wired' bead chain. */
  readonly conductorSpine: string;
  /** Outline drawn under an 'insulated-wire' stroke so it reads as jacketed. */
  readonly insulatedOutline: string;
  /** Small fillet dot marking the electrical endpoints of a 'bare-wire'. */
  readonly solderFillet: string;
  /** Selection highlight stroke. */
  readonly selection: string;
  /** DRC R5' solder-trace proximity risk ring. */
  readonly riskMarker: string;
  /** Component body outline fill. */
  readonly componentBody: string;
  /** Component body outline stroke. */
  readonly componentBodyStroke: string;
  /** Component pin marker dot. */
  readonly pinMarker: string;
  /** Component ref label text colour. */
  readonly label: string;
}

const CONDUCTOR_COLORS_LIGHT: Readonly<Record<ConductorKind, string>> = {
  'lead-bend': '#b6b0a2',
  'solder-trace': '#9a9aa0',
  'solder-trace-wired': '#9a9aa0',
  'bare-wire': '#c8c2b0',
  'insulated-wire': '#3a7bd5',
  'top-jumper': '#d5453a',
  strip: '#c98a3e',
};

export const DEFAULT_THEME: Theme = {
  canvasBackground: '#e9ebee',
  substrate: {
    FR4: '#1c6b3c',
    FR2: '#b8875a',
    FR1: '#a97847',
  },
  pad: '#cc9152',
  hole: '#141414',
  silk: '#f2f2ea',
  conductor: CONDUCTOR_COLORS_LIGHT,
  conductorSpine: '#54545a',
  insulatedOutline: '#1c2b3a',
  solderFillet: '#e0d8c0',
  selection: '#ffb020',
  riskMarker: '#ff3b30',
  componentBody: 'rgba(20,20,20,0.16)',
  componentBodyStroke: '#232323',
  pinMarker: '#555555',
  label: '#f2f2ea',
};

export const DARK_THEME: Theme = {
  ...DEFAULT_THEME,
  canvasBackground: '#14161a',
  componentBody: 'rgba(0,0,0,0.28)',
  componentBodyStroke: '#dcdcdc',
  pinMarker: '#c9c9c9',
  selection: '#ffc84a',
};
