#!/usr/bin/env bash
# Push this folder to a GitHub repo (public by default) and publish the image ghcr.io/<owner>/a-share-watch-cloud:latest.
#   Needs only:  GH_TOKEN=<GitHub classic PAT with scopes: repo, workflow, write:packages>  ./deploy.sh
# Optional env: REPO_NAME (default a-share-watch), VISIBILITY public|private (default public: repo holds no token or personal data)
# After it finishes:
#   - ClawCloud Run (primary): image is pulled from GHCR (private -> give ClawCloud a read:packages token, see DEPLOY.md)
#   - Render (backup): connect the repo once in the Render dashboard (Blueprint render.yaml); later pushes auto-deploy.
# The access token is NOT committed or baked into the image; set ACCESS_TOKEN in the platform's env vars.
set -euo pipefail
cd "$(dirname "$0")"
: "${GH_TOKEN:?set GH_TOKEN (GitHub PAT: repo, workflow, write:packages)}"
export GH_TOKEN
REPO_NAME=${REPO_NAME:-a-share-watch}
OWNER=$(gh api user -q .login)
OWNER_LC=$(echo "$OWNER" | tr '[:upper:]' '[:lower:]')
IMG=ghcr.io/$OWNER_LC/a-share-watch-cloud
echo "GitHub user: $OWNER"
gh repo view "$OWNER/$REPO_NAME" >/dev/null 2>&1 || gh repo create "$OWNER/$REPO_NAME" --${VISIBILITY:-public} -d "A股盯盘 (cloud deploy)" >/dev/null
[ -d .git ] || git init -q -b main
git add -A
git -c user.name="$OWNER" -c user.email="$OWNER@users.noreply.github.com" commit -qm "deploy $(date +%F_%T)" || true
git remote remove origin 2>/dev/null || true
git remote add origin "https://x-access-token:${GH_TOKEN}@github.com/$OWNER/$REPO_NAME.git"
git push -q -f origin main
git remote set-url origin "https://github.com/$OWNER/$REPO_NAME.git"   # don't leave the token in .git/config
echo "pushed -> https://github.com/$OWNER/$REPO_NAME"
# Build+push the image right away if docker works here; otherwise the GitHub Action (.github/workflows/image.yml) does it (~3 min).
DOCKER="docker"; $DOCKER info >/dev/null 2>&1 || DOCKER="sudo -n docker"
if $DOCKER info >/dev/null 2>&1; then
  echo "$GH_TOKEN" | $DOCKER login ghcr.io -u "$OWNER" --password-stdin >/dev/null
  $DOCKER build --network=host -t "$IMG:latest" . && $DOCKER push "$IMG:latest"
  $DOCKER logout ghcr.io >/dev/null
  echo "image pushed: $IMG:latest"
else
  echo "docker unavailable here; GitHub Actions is building $IMG:latest  (watch: gh run list -R $OWNER/$REPO_NAME)"
fi
cat <<MSG

NEXT
  ClawCloud Run  -> App Launchpad: image $IMG:latest (Private, user $OWNER, password = a read:packages PAT), port 8080,
                    env ACCESS_TOKEN=<token>, Local Storage /data, Public access ON.  To roll out a new build: Update/Restart the app.
  Render         -> dashboard: New > Blueprint > repo $OWNER/$REPO_NAME (render.yaml), set ACCESS_TOKEN. Auto-deploys on push.
MSG
