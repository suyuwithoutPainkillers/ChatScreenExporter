---
name: System Utility Core
colors:
  surface: '#f8f9fb'
  surface-dim: '#d9dadc'
  surface-bright: '#f8f9fb'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f3f4f6'
  surface-container: '#edeef0'
  surface-container-high: '#e7e8ea'
  surface-container-highest: '#e1e2e4'
  on-surface: '#191c1e'
  on-surface-variant: '#3e484d'
  inverse-surface: '#2e3132'
  inverse-on-surface: '#f0f1f3'
  outline: '#6e797e'
  outline-variant: '#bdc8ce'
  surface-tint: '#006780'
  primary: '#00647c'
  on-primary: '#ffffff'
  primary-container: '#007f9d'
  on-primary-container: '#fafdff'
  inverse-primary: '#6cd3f7'
  secondary: '#555f6d'
  on-secondary: '#ffffff'
  secondary-container: '#d6e0f1'
  on-secondary-container: '#596372'
  tertiary: '#894e00'
  on-tertiary: '#ffffff'
  tertiary-container: '#a86516'
  on-tertiary-container: '#fffbff'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#b7eaff'
  primary-fixed-dim: '#6cd3f7'
  on-primary-fixed: '#001f28'
  on-primary-fixed-variant: '#004e61'
  secondary-fixed: '#d9e3f4'
  secondary-fixed-dim: '#bdc7d8'
  on-secondary-fixed: '#121c28'
  on-secondary-fixed-variant: '#3e4755'
  tertiary-fixed: '#ffdcbf'
  tertiary-fixed-dim: '#ffb873'
  on-tertiary-fixed: '#2d1600'
  on-tertiary-fixed-variant: '#6a3b00'
  background: '#f8f9fb'
  on-background: '#191c1e'
  surface-variant: '#e1e2e4'
typography:
  h1:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
    letterSpacing: -0.02em
  h2:
    fontFamily: Inter
    fontSize: 20px
    fontWeight: '600'
    lineHeight: 28px
    letterSpacing: -0.01em
  h3:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '600'
    lineHeight: 24px
  body-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  body-sm:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 18px
  label-md:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
    letterSpacing: 0.02em
  label-sm:
    fontFamily: Inter
    fontSize: 11px
    fontWeight: '500'
    lineHeight: 14px
    letterSpacing: 0.03em
  code:
    fontFamily: jetbrainsMono
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 18px
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  base: 4px
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 32px
  gutter: 12px
  margin-page: 24px
---

## Brand & Style
This design system is engineered for productivity-focused Windows desktop applications. It prioritizes utility, density, and clarity over marketing flair. The brand personality is disciplined, systematic, and reliable, evoking the feeling of a native operating system tool.

The visual style is **Corporate / Modern**, leaning heavily into a "Utility-First" aesthetic. It utilizes a layered surface approach where functional zones are clearly demarcated by subtle shifts in value and thin, architectural borders. The interface remains quiet to allow the user's data and tasks to remain the primary focus, ensuring high efficiency during prolonged professional use.

## Colors
The palette is rooted in a professional grayscale spectrum to provide a calm, low-strain working environment. 
- **Primary:** Deep Teal (#0891B2) is reserved for primary actions, active states, and progress indicators. It provides a sharp, high-contrast focal point against the neutral backdrop.
- **Background:** A soft gray (#F3F4F6) acts as the application's base "canvas," reducing the harshness of pure white during extended use.
- **Surface:** Pure white (#FFFFFF) is used for interactive cards, data tables, and input areas to signify "workable" zones.
- **Borders:** A consistent, low-contrast gray (#E5E7EB) defines the structure without creating visual noise.

## Typography
The system uses **Inter** as the primary typeface, chosen for its exceptional legibility and neutral tone. It is optimized for Chinese characters by ensuring standard system fallbacks (such as Microsoft YaHei or Noto Sans SC) align with the x-height of Inter.

The typographic scale is compact. Text sizes are smaller than consumer-web standards to accommodate data-dense layouts. Headlines are kept modest in size, using weight rather than scale to denote hierarchy. A monospaced font (JetBrains Mono) is included for technical data, paths, or configuration strings.

## Layout & Spacing
The layout follows a **Fixed-Fluid Hybrid** model typical of desktop utilities. Sidebars and navigation panels are fixed width (240px or 64px collapsed), while the main content area fluidly adapts to the window size.

A strict **4px baseline grid** governs all spacing. This "compact density" ensures that large amounts of information can be viewed without excessive scrolling. 
- **Gutters:** Standardized at 12px for internal component grouping.
- **Margins:** Page-level margins are 24px, providing a clear frame for the application content.
- **Alignment:** All elements should be top-left aligned by default, following a logical reading gravity for productivity tools.

## Elevation & Depth
Depth is communicated through **Tonal Layering** and **Low-Contrast Outlines** rather than heavy shadows.
- **Level 0 (Background):** #F3F4F6 — The application base.
- **Level 1 (Surface):** #FFFFFF — Main content cards and panels. These use a 1px solid border of #E5E7EB.
- **Level 2 (Interactive):** Elements that hover or are active receive a very subtle, diffused shadow (0 2px 4px rgba(0,0,0,0.05)) to suggest "lift" without breaking the flat utility aesthetic.
- **Level 3 (Popovers):** Modals and context menus use a slightly more pronounced shadow and a slightly darker border (#D1D5DB) to ensure they stand out against the white surfaces below.

## Shapes
The shape language is strictly defined by a **8px (0.5rem) corner radius** for all primary UI components like cards, buttons, and input fields. This radius strikes a balance between the modern Windows "Mica" aesthetic and the precision of a professional tool. 

Smaller elements, like checkboxes or inner tags, may use a reduced 4px radius to maintain visual proportion. Circular shapes are reserved strictly for user avatars or specific toggle indicators.

## Components
- **Buttons:** Primary buttons use the Deep Teal fill with white text. Secondary buttons use a white fill with a #E5E7EB border. Button height is standardized to 32px for high-density layouts.
- **Input Fields:** Use a white background, 1px border (#E5E7EB), and 8px padding. On focus, the border changes to Deep Teal with a 1px inner glow.
- **Cards:** Simple white containers with 1px borders. No internal padding is wasted; content should sit 16px from the card edge.
- **Data Tables:** High-density rows (32px - 36px height) with subtle horizontal dividers. Zebra-striping is encouraged for tables exceeding 10 rows.
- **Chips/Badges:** Small, 20px height tags with a light gray background (#E5E7EB) and semi-bold labels. Status indicators (Success, Error, Warning) use low-saturation background tints with high-saturation text.
- **Lists:** Sidebar navigation items use a 4px left-accent bar in Deep Teal to indicate the active state, accompanied by a subtle #F9FAFB background fill.