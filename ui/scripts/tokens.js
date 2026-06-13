/* ==========================================================================
   FieldSight Design Tokens · JS Mirror
   --------------------------------------------------------------------------
   This file mirrors styles/tokens.css value-for-value so the existing
   FieldSight frontend (which uses inline JSX styles) can read the same
   token values without going through CSS custom properties.

   Hybrid Path C: tokens.css and tokens.js MUST stay in lockstep. If you
   change a hex / size / duration in one, change it in the other in the
   same commit.

   Section numbering matches tokens.css.
   Module format: ES modules.
   ========================================================================== */


/* =======================================================================
   §1 · COLOR PRIMITIVES
   ======================================================================= */

export const tokens = {
  colors: {
    /* Brand · Primary Navy */
    primary: {
      50:  '#F0F4F8',
      100: '#D9E2EC',
      200: '#BCCCDC',
      300: '#9FB3C8',
      400: '#829AB1',
      500: '#627D98',
      600: '#486581',
      700: '#334E68',
      800: '#243B53',
      900: '#102A43',
      950: '#0A1A2E',
    },

    /* Accent · Safety Orange */
    accent: {
      50:  '#FFF4ED',
      100: '#FFE6D5',
      200: '#FFC8A5',
      300: '#FFA06B',
      400: '#FF7A3C',
      500: '#FF6B35',
      600: '#E54B1E',
      700: '#C23616',
      800: '#9A2A13',
      900: '#7A2210',
    },

    /* Semantic · Danger (Red) */
    danger: {
      50:  '#FEF2F2',
      100: '#FEE2E2',
      200: '#FECACA',
      300: '#FCA5A5',
      400: '#F87171',
      500: '#EF4444',
      600: '#DC2626',
      700: '#B91C1C',
      800: '#991B1B',
      900: '#7F1D1D',
    },

    /* Semantic · Warning (Amber) */
    warning: {
      50:  '#FFFBEB',
      100: '#FEF3C7',
      200: '#FDE68A',
      300: '#FCD34D',
      400: '#FBBF24',
      500: '#F59E0B',
      600: '#D97706',
      700: '#B45309',
      800: '#92400E',
      900: '#78350F',
    },

    /* Semantic · Success (Green) */
    success: {
      50:  '#F0FDF4',
      100: '#DCFCE7',
      200: '#BBF7D0',
      300: '#86EFAC',
      400: '#4ADE80',
      500: '#22C55E',
      600: '#16A34A',
      700: '#15803D',
      800: '#166534',
      900: '#14532D',
    },

    /* Semantic · Info (Blue) */
    info: {
      50:  '#EFF6FF',
      100: '#DBEAFE',
      200: '#BFDBFE',
      300: '#93C5FD',
      400: '#60A5FA',
      500: '#3B82F6',
      600: '#2563EB',
      700: '#1D4ED8',
      800: '#1E40AF',
      900: '#1E3A8A',
    },

    /* Neutral */
    neutral: {
      0:   '#FFFFFF',
      50:  '#F9FAFB',
      100: '#F3F4F6',
      200: '#E5E7EB',
      300: '#D1D5DB',
      400: '#9CA3AF',
      500: '#6B7280',
      600: '#4B5563',
      700: '#374151',
      800: '#1F2937',
      900: '#111827',
      950: '#030712',
    },

    /* §2 · Category */
    category: {
      safety:     '#EF4444',
      public:     '#D97706',
      quality:    '#2563EB',
      programme:  '#7C3AED',
      commercial: '#15803D',
      weather:    '#0891B2',
      general:    '#6B7280',

      safetyBg:     '#FEE2E2',
      publicBg:     '#FEF3C7',
      qualityBg:    '#DBEAFE',
      programmeBg:  '#EDE9FE',
      commercialBg: '#DCFCE7',
      weatherBg:    '#CFFAFE',
      generalBg:    '#F3F4F6',
    },
  },


  /* =====================================================================
     §13 · SEMANTIC SURFACES (light mode)
     ===================================================================== */
  surface: {
    app:            '#F9FAFB',  /* neutral-50 */
    panel:          '#FFFFFF',  /* neutral-0  */
    panelElevated:  '#FFFFFF',
    panelMuted:     '#F3F4F6',  /* neutral-100 */
    sidebar:        '#102A43',  /* primary-900 */
    sidebarHover:   '#243B53',  /* primary-800 */
    sidebarActive:  '#334E68',  /* primary-700 */
    input:          '#FFFFFF',
    inputHover:     '#F9FAFB',
    inputFocus:     '#FFFFFF',
    overlay:        'rgba(17, 24, 39, 0.5)',
    tooltip:        '#111827',  /* neutral-900 */
    highlight:      '#FFF4ED',  /* accent-50 */
  },


  /* Borders ------------------------------------------------------------ */
  border: {
    subtle:  '#E5E7EB',  /* neutral-200 */
    default: '#D1D5DB',  /* neutral-300 */
    strong:  '#9CA3AF',  /* neutral-400 */
    focus:   '#FF6B35',  /* accent-500  */
    danger:  '#EF4444',  /* danger-500  */
    success: '#22C55E',  /* success-500 */
  },


  /* Text --------------------------------------------------------------- */
  text: {
    primary:      '#111827',  /* neutral-900 */
    secondary:    '#4B5563',  /* neutral-600 */
    tertiary:     '#6B7280',  /* neutral-500 */
    disabled:     '#9CA3AF',  /* neutral-400 */
    placeholder:  '#9CA3AF',
    link:         '#2563EB',  /* info-600    */
    linkHover:    '#1D4ED8',  /* info-700    */
    inverse:      '#FFFFFF',
    inverseMuted: '#D1D5DB',
    danger:       '#B91C1C',  /* danger-700  */
    success:      '#15803D',  /* success-700 */
    warning:      '#B45309',  /* warning-700 */
  },


  /* =====================================================================
     §3 · TYPOGRAPHY
     ===================================================================== */
  typography: {
    fontFamily: {
      sans:    "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif",
      mono:    "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, Monaco, Consolas, 'Courier New', monospace",
      display: "'Inter', sans-serif",
    },
    fontSize: {
      xs:    '0.75rem',   /* 12px */
      sm:    '0.875rem',  /* 14px */
      base:  '1rem',      /* 16px */
      lg:    '1.125rem',  /* 18px */
      xl:    '1.25rem',   /* 20px */
      '2xl': '1.5rem',    /* 24px */
      '3xl': '1.875rem',  /* 30px */
      '4xl': '2.25rem',   /* 36px */
      '5xl': '3rem',      /* 48px */
    },
    fontWeight: {
      regular:  400,
      medium:   500,
      semibold: 600,
      bold:     700,
    },
    lineHeight: {
      tight:   1.25,
      snug:    1.375,
      normal:  1.5,
      relaxed: 1.625,
      loose:   2,
    },
    letterSpacing: {
      tight:  '-0.025em',
      normal: '0',
      wide:   '0.025em',
      wider:  '0.05em',
    },
  },


  /* =====================================================================
     §5 · SPACING
     ===================================================================== */
  space: {
    0:    '0',
    0.5:  '0.125rem',  /* 2px  */
    1:    '0.25rem',   /* 4px  */
    1.5:  '0.375rem',  /* 6px  */
    2:    '0.5rem',    /* 8px  */
    2.5:  '0.625rem',  /* 10px */
    3:    '0.75rem',   /* 12px */
    4:    '1rem',      /* 16px */
    5:    '1.25rem',   /* 20px */
    6:    '1.5rem',    /* 24px */
    8:    '2rem',      /* 32px */
    10:   '2.5rem',    /* 40px */
    12:   '3rem',      /* 48px */
    16:   '4rem',      /* 64px */
    20:   '5rem',      /* 80px */
    24:   '6rem',      /* 96px */
    32:   '8rem',      /* 128px */
  },


  /* §6 · Touch targets ------------------------------------------------- */
  touchTarget: {
    min:     '2.75rem',  /* 44px */
    default: '3rem',     /* 48px */
    large:   '3.5rem',   /* 56px */
  },


  /* §7 · Radius -------------------------------------------------------- */
  radius: {
    none:  '0',
    sm:    '0.25rem',  /* 4px  */
    md:    '0.5rem',   /* 8px  */
    lg:    '0.75rem',  /* 12px */
    xl:    '1rem',     /* 16px */
    '2xl': '1.5rem',   /* 24px */
    full:  '9999px',
  },


  /* §8 · Shadows · navy-tinted ----------------------------------------- */
  shadow: {
    xs:    '0 1px 2px 0 rgba(16, 42, 67, 0.05)',
    sm:    '0 1px 3px 0 rgba(16, 42, 67, 0.10), 0 1px 2px 0 rgba(16, 42, 67, 0.06)',
    md:    '0 4px 6px -1px rgba(16, 42, 67, 0.10), 0 2px 4px -1px rgba(16, 42, 67, 0.06)',
    lg:    '0 10px 15px -3px rgba(16, 42, 67, 0.10), 0 4px 6px -2px rgba(16, 42, 67, 0.05)',
    xl:    '0 20px 25px -5px rgba(16, 42, 67, 0.10), 0 10px 10px -5px rgba(16, 42, 67, 0.04)',
    '2xl': '0 25px 50px -12px rgba(16, 42, 67, 0.25)',
    inner: 'inset 0 2px 4px 0 rgba(16, 42, 67, 0.06)',
  },


  /* §9 · Motion -------------------------------------------------------- */
  duration: {
    instant: '0ms',
    fast:    '100ms',
    base:    '200ms',
    slow:    '300ms',
    slower:  '500ms',
  },
  easing: {
    linear: 'linear',
    in:     'cubic-bezier(0.4, 0, 1, 1)',
    out:    'cubic-bezier(0, 0, 0.2, 1)',
    inOut:  'cubic-bezier(0.4, 0, 0.2, 1)',
    spring: 'cubic-bezier(0.34, 1.56, 0.64, 1)',
    sharp:  'cubic-bezier(0.4, 0, 0.6, 1)',
  },


  /* §10 · Z-index ------------------------------------------------------ */
  zIndex: {
    base:     0,
    raised:   10,
    dropdown: 100,
    sticky:   200,
    fixed:    300,
    backdrop: 400,
    modal:    500,
    popover:  600,
    toast:    700,
    tooltip:  800,
    max:      9999,
  },


  /* §11 · Breakpoints (px values for JS comparisons) ------------------- */
  breakpoint: {
    sm:    640,
    md:    768,
    lg:    1024,
    xl:    1280,
    '2xl': 1536,
  },


  /* §14 · Construction-specific tokens --------------------------------- */
  priority: {
    critical: '#DC2626',  /* danger-600  */
    high:     '#F87171',  /* danger-400  */
    medium:   '#F59E0B',  /* warning-500 */
    low:      '#3B82F6',  /* info-500    */
    none:     '#9CA3AF',  /* neutral-400 */
  },

  status: {
    open:        '#3B82F6',  /* info-500    */
    inProgress:  '#F59E0B',  /* warning-500 */
    blocked:     '#C026D3',  /* fuchsia-600 */
    done:        '#22C55E',  /* success-500 */
    cancelled:   '#9CA3AF',  /* neutral-400 */
    overdue:     '#DC2626',  /* danger-600  */

    /* Background tints (light mode) */
    openBg:        '#DBEAFE',
    inProgressBg:  '#FEF3C7',
    blockedBg:     '#FAE8FF',
    doneBg:        '#DCFCE7',
    cancelledBg:   '#F3F4F6',
    overdueBg:     '#FEE2E2',
  },

  source: {
    programme:    '#7C3AED',  /* category-programme  */
    conversation: '#3B82F6',  /* info-500            */
    report:       '#15803D',  /* category-commercial */
    manual:       '#6B7280',  /* neutral-500         */
    aiSuggested:  '#FF6B35',  /* accent-500          */
  },
};


/* ==========================================================================
   §15 · DARK MODE OVERRIDES
   --------------------------------------------------------------------------
   Only the keys that change in dark mode. Merged with `tokens` via
   getTokens('dark') below.
   ========================================================================== */

export const darkTokens = {
  surface: {
    app:            '#0A1018',
    panel:          '#111827',
    panelElevated:  '#1F2937',
    panelMuted:     '#0F1623',
    sidebar:        '#000814',
    sidebarHover:   '#0A1018',
    sidebarActive:  '#334E68',
    input:          '#111827',
    inputHover:     '#1F2937',
    inputFocus:     '#111827',
    overlay:        'rgba(0, 0, 0, 0.75)',
    tooltip:        '#1F2937',
    highlight:      'rgba(255, 107, 53, 0.12)',
  },

  border: {
    subtle:  '#1F2937',
    default: '#374151',
    strong:  '#4B5563',
  },

  text: {
    primary:      '#F9FAFB',
    secondary:    '#D1D5DB',
    tertiary:     '#9CA3AF',
    disabled:     '#6B7280',
    placeholder:  '#6B7280',
    inverse:      '#111827',
    inverseMuted: '#4B5563',
    danger:       '#F87171',  /* danger-400  */
    success:      '#4ADE80',  /* success-400 */
    warning:      '#FBBF24',  /* warning-400 */
    link:         '#60A5FA',  /* info-400    */
    linkHover:    '#93C5FD',  /* info-300    */
  },

  colors: {
    category: {
      safetyBg:     'rgba(239, 68, 68, 0.15)',
      publicBg:     'rgba(217, 119, 6, 0.15)',
      qualityBg:    'rgba(37, 99, 235, 0.15)',
      programmeBg:  'rgba(124, 58, 237, 0.15)',
      commercialBg: 'rgba(21, 128, 61, 0.15)',
      weatherBg:    'rgba(8, 145, 178, 0.15)',
      generalBg:    'rgba(107, 114, 128, 0.15)',
    },
  },

  status: {
    blocked:    '#F0ABFC',  /* fuchsia-300 */
    blockedBg:  'rgba(192, 38, 211, 0.22)',
    overdueBg:  'rgba(220, 38, 38, 0.22)',
  },
};


/* ==========================================================================
   Helpers
   ========================================================================== */

/* Tiny deep-merge — only objects, no arrays. b's keys win on conflict. */
function deepMerge(a, b) {
  if (b === undefined) return a;
  if (a === null || typeof a !== 'object' || Array.isArray(a)) return b;
  if (b === null || typeof b !== 'object' || Array.isArray(b)) return b;
  const out = { ...a };
  for (const key of Object.keys(b)) {
    out[key] = (key in a) ? deepMerge(a[key], b[key]) : b[key];
  }
  return out;
}

/**
 * Get the active token set for a given mode.
 * @param {'light'|'dark'} mode
 */
export function getTokens(mode = 'light') {
  return mode === 'dark' ? deepMerge(tokens, darkTokens) : tokens;
}

/**
 * Convert a flat tokens object into a CSS-custom-property style block,
 * for components that want to set theme values dynamically.
 * Only emits primitives (colors), not the full tree — extend as needed.
 *
 * @example
 *   <div style={styleFromTokens(getTokens('dark'))}>...</div>
 */
export function styleFromTokens(t) {
  const out = {};
  // Color primitives
  for (const family of ['primary', 'accent', 'danger', 'warning', 'success', 'info', 'neutral']) {
    const ramp = t.colors?.[family];
    if (!ramp) continue;
    for (const [shade, hex] of Object.entries(ramp)) {
      out[`--color-${family}-${shade}`] = hex;
    }
  }
  // Surfaces
  for (const [k, v] of Object.entries(t.surface || {})) {
    out[`--surface-${kebab(k)}`] = v;
  }
  // Text
  for (const [k, v] of Object.entries(t.text || {})) {
    out[`--text-${kebab(k)}`] = v;
  }
  // Borders
  for (const [k, v] of Object.entries(t.border || {})) {
    out[`--border-${kebab(k)}`] = v;
  }
  return out;
}

function kebab(s) {
  return s.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());
}

export default tokens;
