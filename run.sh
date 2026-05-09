#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

pull_branch() {
  local branch="$1"
  git pull --ff-only origin "$branch"
}

# update code if git repo has remotes configured
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git fetch --all --prune || true
  current_branch="$(git rev-parse --abbrev-ref HEAD)"
  if git rev-parse --verify "origin/${current_branch}" >/dev/null 2>&1; then
    if ! pull_branch "$current_branch"; then
      echo "git pull failed, trying one-time stash of local/untracked changes..."
      git stash push -u -m "run.sh autostash before pull" >/dev/null || true
      pull_branch "$current_branch"
      git stash pop >/dev/null || true
    fi
  fi
fi

# stop old stack
(docker compose down --remove-orphans || true)

# force rebuild so Python source updates are guaranteed inside image
DOCKER_BUILDKIT=1 docker compose up -d --build --force-recreate

docker compose ps

echo "\nMiner logs (last 40 lines):"
docker compose logs --tail=40 miner || true

echo "\nWebUI logs (last 40 lines):"
docker compose logs --tail=40 webui || true
