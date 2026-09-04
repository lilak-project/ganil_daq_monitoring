import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { MantineProvider } from "@mantine/core"

import "@mantine/core/styles.css"
import "./index.css"

import App from "./App.tsx"
import { theme } from "./theme.ts"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* Light only: this is read on a lit counting-room wall, and a dark panel
        washes out under the room lights. */}
    <MantineProvider theme={theme} forceColorScheme="light">
      <App />
    </MantineProvider>
  </StrictMode>,
)
