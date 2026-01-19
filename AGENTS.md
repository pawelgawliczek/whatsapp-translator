# Repository Guidelines

## Project Structure & Module Organization
- `docker-compose.yml`: orchestrates the WhatsApp bridge (`whatsapp-bot`) and the translator app (`translator`).
- `translator/`: Python FastAPI service that receives webhooks and calls OpenAI; `app/main.py` contains routing and translation logic.
- `translator/requirements.txt` + `translator/Dockerfile`: dependency and image definitions.
- `creds.json/`: runtime auth material for WhatsApp (kept out of source).

## Build, Test, and Development Commands
- `docker compose up -d --build whatsapp-bot translator`: rebuilds the translator image, restarts both services, and refreshes the webhook container.
- `docker compose logs -f translator`: tails webhook handling output; verify translation results in real time.
- `docker compose exec translator uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`: run the FastAPI server interactively when debugging.
- `docker compose restart whatsapp-bot translator`: quickest way to pick up config or credential changes.

## Coding Style & Naming Conventions
- Python modules use 4-space indentation, snake_case identifiers (`send_text`, `SEEN_IDS`), and grouped imports (stdlib, third-party, project).
- Keep FastAPI endpoints small and pure; push WA/OpenAI helpers into standalone functions for reuse.
- Environment variables appear in uppercase snake case (`OPENAI_MODEL`, `WA_API_BASE`); document new ones in `docker-compose.yml`.

## Testing Guidelines
- No automated suite exists yet; rely on manual verification via WhatsApp groups plus translator logs.
- Exercise routes with `curl` or `docker compose exec translator python - <<'PY' ...` snippets; include example inputs/outputs in PR descriptions.
- When changing translation prompts, capture before/after samples to prove behavior.

## Commit & Pull Request Guidelines
- Follow imperative, descriptive commit subjects (e.g., `Fix sendText payload schema`, `Skip media messages`).
- PRs should explain motivation, the fix, and manual verification steps; attach log excerpts or screenshots where helpful.
- Link related issues and note any secrets/infra updates required for rollout.

## Security & Configuration Tips
- Never commit actual `creds.json` contents; mount the directory at deploy time instead.
- Treat `OPENAI_API_KEY` as secret—inject via env vars or docker secrets, not source.
- Before wiping sessions through the WA explorer, coordinate downtime because it forces QR re-auth and clears cached message state.

## WhatsApp Bot Operations & Troubleshooting
- **QR Code URL**: https://wa.pawelgawliczek.cloud (Caddy reverse proxy from `/opt/caddy/Caddyfile`)
- **Port mapping**: external 9001 → internal 8002
- **If bot stops working**:
  1. Check status: `docker compose ps -a`
  2. Check logs: `docker compose logs whatsapp-bot --tail=50`
  3. Simple restart: `docker compose restart`
  4. If stuck at "Authenticating" with no QR: `docker compose up -d --force-recreate whatsapp-bot`
  5. Wait for "First QR" message in logs, then scan at the URL above
- **After scanning QR**: Keep session alive for at least 5 minutes before restarting to persist session data
- **Session data**: Stored in `/usr/src/app/session.data.json` inside container; may require re-scan after container recreation
