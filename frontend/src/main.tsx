import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { MantineProvider } from "@mantine/core"

import "@mantine/core/styles.css"
import "./index.css"
import "@neon/neon.css"
import "./neon-ganil.css"

import App from "./App.tsx"
import { neonTheme } from "@neon/mantine"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* The DAQ look (neon_ui): Mantine stays in its dark scheme, NEON's tokens
        draw it bright or dark. */}
    <MantineProvider theme={neonTheme} forceColorScheme="dark">
      <App />
    </MantineProvider>
  </StrictMode>,
)
