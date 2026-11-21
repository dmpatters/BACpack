# BACpack frontend

The web UI BACpack serves: a React and TypeScript single-page app built with Vite.

## Dev workflow

```
npm install
npm run dev       # Vite dev server on :5173, proxies /api and /docs to
                   # a locally-running `uvicorn bacpack.web.app:app --port 8000`
```

## Building for release

```
npm run build      # tsc -b && vite build -- outputs straight into
                    # ../src/bacpack/web/spa (see vite.config.ts), which
                    # gets committed to git and shipped as package data.
```

There is no "run Vite in production" mode. FastAPI serves the built `../src/bacpack/web/spa` directory directly (see the catch-all route in `app.py`). Commit the rebuilt `spa/` output with any frontend change that should ship.

A frontend commit whose rebuild is byte-identical to the committed `spa/`, such as a comment-only change, can instead say so with a `SPA-unchanged: <reason>` trailer in its commit message.

## Tests

```
npm run test       # Vitest + React Testing Library
```
