'use client'

import { CopilotKit } from '@copilotkit/react-core'
import '@copilotkit/react-ui/styles.css'
import './globals.css'

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>
        <CopilotKit publicLicenseKey="ck_pub_2c442ff631558f973462c4c5f29e4ce5" runtimeUrl="/api/copilotkit">
          {children}
        </CopilotKit>
      </body>
    </html>
  )
}
