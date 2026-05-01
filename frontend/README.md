# Frontend

Next.js 16 / React 19 / Tailwind CSS 4 / TypeScript 5

Interactive wizard UI that guides users through the product deployment lifecycle: requirements gathering, design selection, IaC generation, and documentation.

## Quick Start

```bash
pnpm install          # install deps
pnpm dev              # dev server → http://localhost:3000
```

Requires the backend running on `http://localhost:8000` (see `../dev.sh` to start both).

## Project Structure

```
src/
├── app/
│   ├── page.tsx                    # Dashboard — project list + creation
│   ├── project/[id]/page.tsx       # 4-step wizard workspace
│   ├── layout.tsx                  # Root layout (Geist fonts, metadata)
│   └── error.tsx                   # Global error boundary
├── components/
│   ├── wizard/                     # Wizard step components
│   │   ├── RequirementsForm.tsx    # Step 1: use case, bandwidth, compliance
│   │   ├── InterviewChat.tsx       # Step 1b: AI-driven requirements refinement
│   │   ├── DesignReview.tsx        # Step 2: compare architecture options
│   │   ├── DeploymentParametersForm.tsx  # Step 2b: CIDR, region, env
│   │   ├── IaCView.tsx             # Step 3: generated CloudFormation files
│   │   └── DocumentationView.tsx   # Step 4: user guide, threat model, diagram
│   ├── ui/                         # Reusable components
│   │   ├── CodeBlock.tsx           # Syntax-highlighted code (Prism)
│   │   ├── MarkdownRenderer.tsx    # Markdown → React with Tailwind prose
│   │   └── MermaidRenderer.tsx     # Architecture diagrams from Mermaid
│   └── dashboard/
│       └── PhaseIndicator.tsx      # Visual step progress indicator
├── hooks/
│   ├── useWizardState.ts           # Central state (useReducer + hydration)
│   └── useWebSocket.ts            # Real-time task updates
└── lib/
    ├── api.ts                      # REST + SSE streaming client
    └── types.ts                    # TypeScript types (mirrors backend Pydantic)
```

## Wizard Flow

```
Dashboard → Create Project
  ↓
Step 1: Requirements    → Form input + optional AI interview chat (SSE)
  ↓
Step 2: Design          → Review 2-3 architecture options → Select + refine parameters
  ↓
Step 3: IaC             → View generated CloudFormation files + validation report
  ↓
Step 4: Documentation   → User guide, threat model, architecture diagram
```

## State Management

Central state lives in `useWizardState.ts` using `useReducer` with discriminated union actions. Key features:

- **Hydration**: On mount, fetches full project state from backend to resume where the user left off
- **Async polling**: Polls task status with max attempt limits (design: 60x3s, docs: 120x3s)
- **WebSocket**: Receives real-time updates for design/IaC/docs task completion
- **Fallback**: Polling continues if WebSocket disconnects

## Backend Communication

| Method | Channel | Used For |
|--------|---------|----------|
| REST | `fetch` | Project CRUD, task submission, design selection |
| SSE | `EventSource` | Interview chat streaming |
| WebSocket | `useWebSocket` | Real-time task completion/failure notifications |
| Polling | `setInterval` | Fallback for task status when WebSocket unavailable |

API client is in `src/lib/api.ts`. All calls include `tenant_id` for multi-tenancy.

## Environment Variables

In `.env.local`:

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXT_PUBLIC_BACKEND_URL` | `http://localhost:8000` | Backend API base URL |
| `NEXT_PUBLIC_WEBSOCKET_URL` | `ws://localhost:8000/ws` | WebSocket endpoint |

## Building for Production

### Static Export (S3 + CloudFront)

```bash
NEXT_OUTPUT=export pnpm build    # generates static files in out/
```

The `NEXT_OUTPUT=export` env var triggers Next.js static export mode. The generated `out/` directory can be deployed to S3 + CloudFront. Security headers are handled by CloudFront response headers policy (see `infra/lib/cloudfront.ts`).

### Server Mode

```bash
pnpm build && pnpm start    # runs Next.js server on port 3000
```

In server mode, security headers are applied via `next.config.ts` `headers()` function (CSP, HSTS, X-Frame-Options, etc.).

## Commands

```bash
pnpm dev       # development server (port 3000, hot reload)
pnpm build     # production build
pnpm start     # production server (requires build first)
pnpm lint      # ESLint
```
