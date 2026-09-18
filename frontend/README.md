# Medical Nudging Pilot - Frontend

A React-based clinical decision support dashboard for viewing patient data, generating medical nudges, and reviewing inference results.

## Features

- **Nudge Generation**: Load patient records (CCDA/FHIR), configure visit context, generate AI-powered clinical nudges
- **Results Viewer**: Browse pre-computed inference results with execution traces
- **Admin**: View system prompts and execution traces for debugging

## Prerequisites

- Node.js 20.19+ (required by Vite 7 and @vitejs/plugin-react 5; Node 18 builds with EBADENGINE warnings)
- npm 9+

## Installation

```bash
cd frontend
npm install
```

## Running the Full Application

The application requires both the backend API and frontend to be running.

### Terminal 1: Start Backend API

```bash
# From project root
uv run uvicorn medical_nudging.api.main:app --reload --port 8000
```

Backend API available at http://localhost:8000

### Terminal 2: Start Frontend

```bash
cd frontend
npm run dev
```

Frontend available at http://localhost:5173

The frontend proxies `/api` requests to the backend at port 8000.

### Quick Start (Both Services)

```bash
# Terminal 1 - Backend
uv run uvicorn medical_nudging.api.main:app --reload --port 8000

# Terminal 2 - Frontend
cd frontend && npm run dev
```

## Development (Frontend Only)

For frontend-only development (Results Viewer works without backend):

```bash
npm run dev
```

**Note:** Nudge Generation and Admin pages require the backend to be running.

## Production Build

```bash
npm run build
npm run preview  # Preview at http://localhost:4173
```

## Viewing Sample Results

1. Start dev server: `npm run dev`
2. Navigate to **Results** tab
3. Select inference run from dropdown
4. Click patient samples to view nudges and traces

## Testing

```bash
npx playwright install chromium  # First time only
npx playwright test
npx playwright test --ui         # Interactive mode
```

## Project Structure

```
frontend/
├── src/
│   ├── api/           # Backend API client
│   ├── components/    # React components
│   ├── hooks/         # Custom hooks
│   ├── pages/         # Page components
│   ├── types/         # TypeScript definitions
│   └── index.css      # Tailwind CSS styles
├── public/results/    # Sample inference results
└── tests/             # Playwright e2e tests
```

## Tech Stack

React 19, TypeScript 5.9, Vite 7, Tailwind CSS 4, TanStack Query, Recharts, Playwright
