import React from 'react'
import ReactDOM from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'

import { App } from './App'
import './index.css'
import { Channels } from './pages/Channels'
import { Ingest } from './pages/Ingest'
import { JobProgress } from './pages/JobProgress'
import { Jobs } from './pages/Jobs'
import { Queue } from './pages/Queue'
import { Review } from './pages/Review'
import { Settings } from './pages/Settings'
import { Setup } from './pages/Setup'

const router = createBrowserRouter([
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <Ingest /> },
      { path: 'jobs', element: <Jobs /> },
      { path: 'jobs/:jobId', element: <JobProgress /> },
      { path: 'jobs/:jobId/clips', element: <Review /> },
      { path: 'queue', element: <Queue /> },
      { path: 'channels', element: <Channels /> },
      { path: 'settings', element: <Settings /> },
      { path: 'setup', element: <Setup /> },
    ],
  },
])

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
)
