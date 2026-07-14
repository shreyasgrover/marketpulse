#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  MarketPulse — one-command GitHub setup
#  Creates the repo, pushes this folder, sets the Finnhub secret,
#  enables GitHub Pages, and triggers the first data runs.
#
#  Requires: GitHub CLI →  brew install gh
#  Usage:    ./setup.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

REPO_NAME="${1:-marketpulse}"
cd "$(dirname "$0")"

say()  { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m  ✓ %s\033[0m\n" "$*"; }
fail() { printf "\033[1;31m  ✗ %s\033[0m\n" "$*"; exit 1; }

# ── Preflight ────────────────────────────────────────────────────
command -v gh  >/dev/null || fail "GitHub CLI not found. Install with:  brew install gh"
command -v git >/dev/null || fail "git not found."
gh auth status >/dev/null 2>&1 || { say "Logging you into GitHub…"; gh auth login; }
GH_USER=$(gh api user -q .login)
ok "Authenticated as $GH_USER"

# ── Git repo ─────────────────────────────────────────────────────
say "Preparing local git repo"
[ -d .git ] || git init -q
git add -A
git diff --cached --quiet || git commit -q -m "MarketPulse: pipeline + dashboard + automation"
git branch -M main
ok "Local commit ready"

# ── Create + push ────────────────────────────────────────────────
say "Creating github.com/$GH_USER/$REPO_NAME (public — required for free Pages)"
if gh repo view "$GH_USER/$REPO_NAME" >/dev/null 2>&1; then
  ok "Repo already exists — pushing to it"
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$GH_USER/$REPO_NAME.git"
  git push -u origin main
else
  gh repo create "$REPO_NAME" --public --source=. --push
fi
ok "Code pushed"

# ── Secret ───────────────────────────────────────────────────────
say "Finnhub API key (free at https://finnhub.io/register)"
printf "  Paste key (or press Enter to skip — news/sentiment will be disabled): "
read -r -s FINNHUB_KEY; echo
if [ -n "$FINNHUB_KEY" ]; then
  gh secret set FINNHUB_API_KEY --repo "$GH_USER/$REPO_NAME" --body "$FINNHUB_KEY"
  ok "Secret FINNHUB_API_KEY set"
else
  ok "Skipped — add later via repo Settings → Secrets → Actions"
fi

# ── GitHub Pages ─────────────────────────────────────────────────
say "Enabling GitHub Pages (main branch, root)"
gh api "repos/$GH_USER/$REPO_NAME/pages" -X POST \
  -f "source[branch]=main" -f "source[path]=/" >/dev/null 2>&1 \
  && ok "Pages enabled" \
  || ok "Pages already enabled (or enable manually: Settings → Pages)"

# ── First runs ───────────────────────────────────────────────────
say "Triggering first data runs"
sleep 3   # let GitHub index the workflows
gh workflow run "momentum_pipeline.yml"  --repo "$GH_USER/$REPO_NAME" && ok "Daily pipeline started (~15-30 min)"
gh workflow run "backtest_history.yml"   --repo "$GH_USER/$REPO_NAME" && ok "Backtest history started (~5-10 min)"

# ── Done ─────────────────────────────────────────────────────────
cat <<EOF

═══════════════════════════════════════════════════════════════
  🎉 Setup complete!

  Dashboard (live ~30 min after the pipeline finishes):
    https://$GH_USER.github.io/$REPO_NAME/

  Watch progress:
    https://github.com/$GH_USER/$REPO_NAME/actions

  From now on everything refreshes automatically:
    • Momentum + risk data — weekdays 22:00 UTC
    • Backtest price history — Sundays 10:00 UTC
═══════════════════════════════════════════════════════════════
EOF
