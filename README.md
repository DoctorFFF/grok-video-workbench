---
title: Grok Video Workbench
emoji: 🎬
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
license: cc-by-nc-4.0
---

# Grok Video Workbench

Grok Video Workbench is a browser-based workbench for planning, submitting,
tracking, downloading, and merging multi-scene Grok/xAI video generation jobs
through a compatible CPA/CLIProxyAPI gateway.

License: **CC BY-NC 4.0**. Commercial use is prohibited.

## What It Is For

This project is designed for people who need a private, operator-friendly video
generation console instead of a one-shot prompt box.

It is useful for:

- Building a multi-scene video from ordered story beats.
- Managing prompt, image reference, duration, resolution, and aspect ratio per scene.
- Submitting scenes with controlled spacing while generation runs concurrently.
- Tracking request IDs, remote video URLs, download state, and merge history.
- Manually uploading videos when remote downloads require another network.
- Managing xAI/Grok auth files exposed by a CPA/CLIProxyAPI-compatible backend.
- Running a self-hosted personal tool behind an application-level login gate.

It is not intended as:

- A public anonymous video generation service.
- A replacement for your CPA/CLIProxyAPI access-control layer.
- A place to commit real API keys, auth files, task data, generated videos, or tokens.

## Security Model

The app has its own initial authorization-key gate.

- Before login, all non-auth API routes are blocked.
- The login key is hashed with PBKDF2; the original key is not stored.
- Successful login sets an HMAC-signed, HttpOnly cookie.
- CPA secrets should be supplied through environment variables or platform secrets.
- The frontend does not receive `CPA_API_KEY` or `CPA_MANAGEMENT_KEY` from bootstrap/settings APIs.
- Runtime data belongs in `data/` and `videos/`, which are intentionally ignored by git.

Required secret values:

| Variable | Purpose |
| --- | --- |
| `WORKBENCH_AUTH_KEY` | Login key for this workbench. Set this before public deployment. |
| `CPA_BASE_URL` | Base URL of your CPA/CLIProxyAPI service. |
| `CPA_API_KEY` | API key used for generation requests. |
| `CPA_MANAGEMENT_KEY` | Management key used for xAI auth-file operations. |

Optional image-host variables:

| Variable | Default |
| --- | --- |
| `IMAGE_HOST_BASE_URL` | `https://img.remit.ee` |
| `IMAGE_HOST_SELECTED_URL` | same as `IMAGE_HOST_BASE_URL` |
| `IMAGE_HOST_OPTIONS` | comma-separated list of image-host base URLs |

Never commit a real `.env`, `data/`, `videos/`, auth JSON files, downloaded
videos, or platform access tokens.

## Quick Start With Docker Compose

1. Clone the repository.

```bash
git clone https://github.com/<your-user>/grok-video-workbench.git
cd grok-video-workbench
```

2. Create a local `.env` file from the template.

```bash
cp .env.example .env
```

3. Edit `.env` and fill in your own values.

```dotenv
WORKBENCH_AUTH_KEY=replace-with-a-long-private-login-key
CPA_BASE_URL=https://your-cpa.example.com
CPA_API_KEY=replace-with-your-cpa-api-key
CPA_MANAGEMENT_KEY=replace-with-your-cpa-management-key
```

4. Start the service.

```bash
docker compose up -d --build
```

5. Open the app.

```text
http://127.0.0.1:7860
```

6. Enter the `WORKBENCH_AUTH_KEY` value to unlock the workbench.

Useful commands:

```bash
docker compose logs -f
docker compose restart
docker compose down
```

Runtime files are stored in:

```text
data/    # auth config, settings, task state
videos/  # downloaded, uploaded, and merged videos
```

## Local Python Run

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Start the app:

```bash
python app.py
```

The default local port is `8765`. You can override it:

```bash
PORT=8766 python app.py
```

For public use, prefer Docker or a platform that supports environment secrets.

## Hugging Face Docker Space Deployment

This repository is compatible with Hugging Face Docker Spaces.

Recommended setup:

1. Create a new Space with `sdk: docker`.
2. Add these as Space Secrets:
   - `WORKBENCH_AUTH_KEY`
   - `CPA_BASE_URL`
   - `CPA_API_KEY`
   - `CPA_MANAGEMENT_KEY`
3. Add optional image-host values as Space Variables if needed.
4. Keep the Space public only if you are comfortable exposing the UI and source
   code. The app-level login still protects backend operations.

Do not upload local runtime folders to the Space repository:

```text
data/
videos/
tools/ffmpeg/
.env
```

## Workflow

1. Create a task from the left panel.
2. Add scenes in the center panel.
3. For each scene, enter a prompt and optional image URLs.
4. Choose model, duration, resolution, and aspect ratio.
5. Submit the task. Scenes are submitted in order, while generation can proceed concurrently.
6. Refresh or inspect scene results as request IDs and remote URLs appear.
7. Download generated videos, or manually upload a video when automatic download is unavailable.
8. Select downloaded/uploaded scenes and merge them into one final video.

## Model Routing

The app currently supports:

| Model | Text input | Image input |
| --- | --- | --- |
| `grok-imagine-video` | yes | yes |
| `grok-imagine-video-1.5-preview` | no | yes |

If a scene includes image URLs, the backend routes to an image-capable model.
If the selected model is incompatible with the scene input, the backend records
the actual model used.

## Manual Download Fallback

Some remote video URLs may require a network environment different from the
server running this app. If automatic download fails:

1. Copy the remote video URL from the scene.
2. Download it manually in a network that can access the URL.
3. Return to the workbench.
4. Click the scene upload button.
5. Upload the downloaded file.

Supported upload formats:

- `.mp4`
- `.mov`
- `.webm`
- `.mkv`

Non-MP4 files are converted to MP4 when ffmpeg is available.

## ffmpeg

Video validation, conversion, and merging require ffmpeg/ffprobe.

The Docker image installs ffmpeg automatically. For local Python runs, install
ffmpeg on your system or place a local ffmpeg build under:

```text
tools/ffmpeg/
```

## CPA/xAI Management Scope

The workbench intentionally limits management operations to xAI/Grok auth files.

Supported operations:

- List xAI auth files.
- Refresh a single xAI account view.
- Enable or disable one xAI auth file.
- Download a backup of one xAI auth file.
- Delete one xAI auth file.
- Upload `xai-*.json` auth files.

Safety boundaries:

- No all-provider refresh.
- No global delete action.
- No CPA `usage-queue` call.
- No display of raw xAI bearer tokens.
- No handling of non-xAI provider files.

## Repository Layout

```text
app.py              # FastAPI backend and workbench logic
static/             # HTML, CSS, and browser-side JavaScript
Dockerfile          # Docker Space and container image
docker-compose.yml  # Local Docker Compose deployment
.env.example        # Safe placeholder environment template
requirements.txt    # Python dependencies
LICENSE.md          # CC BY-NC 4.0 license notice
```

Ignored runtime paths:

```text
data/
videos/
tools/ffmpeg/
.env
```

## Troubleshooting

Login succeeds but the page returns to the key screen:

- Make sure the browser is visiting the same origin, not mixing custom domains and direct Space URLs.
- Clear old cookies for the host and retry.
- On hosted deployments, use HTTPS so the secure login cookie can be stored.

The app opens but all CPA operations fail:

- Confirm `CPA_BASE_URL`, `CPA_API_KEY`, and `CPA_MANAGEMENT_KEY`.
- Confirm your CPA service is reachable from the container or hosting platform.
- Check `docker compose logs -f` for upstream HTTP errors.

Video merge fails:

- Confirm all selected scenes have local videos.
- Install ffmpeg or use the Docker image.
- Enable normalization when source videos have different resolution, frame rate, or codec.
