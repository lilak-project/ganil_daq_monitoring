import { Badge, Container, createTheme, Paper, rem } from "@mantine/core"

/**
 * Theme for the GANIL DAQ panel.
 *
 * The same reasoning as the vacuum and actuator panels it sits beside: this is
 * read from a metre away in a lit counting room, so type is large and the
 * accent colour is spent sparingly. Nothing here drives hardware -- there is no
 * control to spend it on -- so colour carries state only: teal for running,
 * red for down.
 */
/** Wide: the run table wants a note column that does not wrap every line. */
export const CONTENT_WIDTH = 1320

export const theme = createTheme({
  // Violet 7 is #7048e8 -- the same value the portal manifest paints this
  // service's tile with, so the tab, the tile and the page agree. State keeps
  // its own colours: teal for a run going, red for a component down. The
  // identity colour is never spent on those.
  primaryColor: "violet",
  primaryShade: 7,
  defaultRadius: "md",

  fontFamily:
    '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
  fontFamilyMonospace: '"Geist Mono Variable", ui-monospace, SFMono-Regular, monospace',
  headings: { fontWeight: "600" },

  // Still a touch larger than Mantine's defaults, for reading at arm's length.
  fontSizes: {
    xs: rem(12),
    sm: rem(14),
    md: rem(15),
    lg: rem(16),
    xl: rem(19),
  },
  lineHeights: { md: "1.55" },
  spacing: { xs: rem(10), sm: rem(14), md: rem(20), lg: rem(28), xl: rem(40) },

  components: {
    Paper: Paper.extend({
      defaultProps: { radius: "lg" },
    }),
    Badge: Badge.extend({
      // Names like "Run control server" are read as words, not shouted.
      styles: { label: { textTransform: "none" } },
    }),
    Container: Container.extend({
      defaultProps: { size: CONTENT_WIDTH },
    }),
  },
})
