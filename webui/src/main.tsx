import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'
import App from './App'
import { installAcceleratedPointRaycast } from './lib/acceleratedPointRaycast'

installAcceleratedPointRaycast()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
