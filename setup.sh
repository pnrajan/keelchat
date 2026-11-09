#!/usr/bin/env bash
# Set up yachat on this machine: checks for what it needs (git, curl, uv, Node 20+; Docker with --docker), offers to
# install what's missing with this system's package manager, writes .env with your model key, and installs the Python
# and console dependencies. macOS (Homebrew), Debian/Ubuntu (apt), Fedora/RHEL (dnf), Arch (pacman) and WSL.
#
#   ./setup.sh             # ask before each install
#   ./setup.sh --yes       # install without asking (CI, a fresh VM)
#   ./setup.sh --check     # only report what's there and what's missing
#   ./setup.sh --docker    # also Docker, for scripts/local.sh (the whole stack in containers)
#
# Then: scripts/try.sh (the console, everything in memory) or scripts/local.sh up (in Docker).
set -euo pipefail
cd "$(dirname "$0")"

YES='' CHECK='' DOCKER=''
for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    --check) CHECK=1 ;;
    --docker) DOCKER=1 ;;
    -h|--help) sed -n 2,10p "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
miss() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
interactive() { [ -t 0 ]; }
SUDO=''; [ "$(id -u)" -eq 0 ] || SUDO='sudo'

# ---- what system this is
OS=$(uname -s) PM='' WSL=''
case "$OS" in
  Darwin) PM=brew ;;
  Linux)
    grep -qi microsoft /proc/version 2>/dev/null && WSL=1
    ids=$(. /etc/os-release 2>/dev/null && echo "${ID:-} ${ID_LIKE:-}")
    case " $ids " in
      *" debian "*|*" ubuntu "*) PM=apt ;;
      *" fedora "*|*" rhel "*|*" centos "*) PM=dnf ;;
      *" arch "*) PM=pacman ;;
    esac ;;
  MINGW*|MSYS*|CYGWIN*) fail "On Windows, run this inside WSL 2 (https://learn.microsoft.com/windows/wsl/install)." ;;
esac
bold "yachat setup: $OS${WSL:+ (WSL)}${PM:+, packages with $PM}"

# ---- installing, always with a yes first (unless --yes)
confirm() {  # confirm "what" "command"
  [ -n "$CHECK" ] && { miss "would install $1: $2"; return 1; }
  [ -n "$YES" ] && return 0
  interactive || { miss "$1 is missing; install it with: $2  (or rerun with --yes)"; return 1; }
  printf '  Install %s with:\n    %s\n  Go ahead? [y/N] ' "$1" "$2"
  read -r answer; [ "$answer" = y ] || [ "$answer" = Y ]
}
install() {  # install "what" "command"
  if confirm "$1" "$2"; then
    bash -c "$2" || fail "installing $1 failed; install it yourself and rerun ./setup.sh"
  else
    MISSING+=("$1"); return 1
  fi
}
pkg() {  # the install command for packages on this system, or empty
  case "$PM" in
    brew) echo "brew install $*" ;;
    apt) echo "$SUDO apt-get update -qq && DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq $*" ;;
    dnf) echo "$SUDO dnf install -y -q $*" ;;
    pacman) echo "$SUDO pacman -S --noconfirm --needed $*" ;;
  esac
}
MISSING=()

if [ "$PM" = brew ] && ! have brew; then
  install Homebrew '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
fi

bold "Tools"
for t in git curl; do
  if have "$t"; then ok "$t"
  elif [ -n "$(pkg "$t")" ]; then install "$t" "$(pkg "$t")"
  else miss "$t: install it with your package manager"; MISSING+=("$t"); fi
done

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"  # where the uv installer puts it
if have uv; then ok "uv $(uv --version | awk '{print $2}')"
else install uv "curl -LsSf https://astral.sh/uv/install.sh | sh"; fi

node_major() { node -v 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/'; }
if have node && [ "$(node_major)" -ge 20 ] && have npm; then ok "node $(node -v)"
else
  have node && miss "node $(node -v) is older than 20"
  case "$PM" in
    brew) install "Node 22" "brew install node@22 && brew link --overwrite --force node@22" ;;
    apt) install "Node 22" "curl -fsSL https://deb.nodesource.com/setup_22.x | ${SUDO:+$SUDO -E }bash - && $SUDO apt-get install -y -qq nodejs" ;;
    dnf) install "Node 22" "$SUDO dnf install -y -q nodejs22 nodejs22-npm 2>/dev/null || $SUDO dnf install -y -q nodejs npm" ;;
    pacman) install "Node" "$SUDO pacman -S --noconfirm --needed nodejs npm" ;;
    *) miss "Node 20+: https://nodejs.org/en/download"; MISSING+=(node) ;;
  esac
  hash -r
  for n in /opt/homebrew/opt/node@22/bin /usr/local/opt/node@22/bin; do [ -d "$n" ] && export PATH="$n:$PATH"; done
fi

if [ -n "$DOCKER" ]; then
  if have docker && docker compose version >/dev/null 2>&1; then
    ok "docker $(docker --version | sed -E 's/Docker version ([^,]+).*/\1/'), compose v2"
    docker info >/dev/null 2>&1 || miss "Docker is installed but not running: start Docker Desktop or the docker service"
  elif [ -n "$WSL" ]; then
    miss "Docker: install Docker Desktop for Windows and turn on WSL integration (https://docs.docker.com/desktop/features/wsl/)"
    MISSING+=(docker)
  else
    case "$PM" in
      brew) install "Docker Desktop" "brew install --cask docker" && miss "open Docker Desktop once so it can finish setting up" ;;
      apt|dnf) install Docker "curl -fsSL https://get.docker.com | $SUDO sh && $SUDO usermod -aG docker \"\$USER\"" \
                 && miss "log out and in again so you can run docker without sudo" ;;
      pacman) install Docker "$SUDO pacman -S --noconfirm --needed docker docker-compose && $SUDO systemctl enable --now docker && $SUDO usermod -aG docker \"\$USER\"" ;;
      *) miss "Docker: https://docs.docker.com/get-docker/"; MISSING+=(docker) ;;
    esac
  fi
fi

if [ -n "$CHECK" ]; then
  [ ${#MISSING[@]} -eq 0 ] && [ -f .env ] && { bold "Everything is in place."; exit 0; }
  [ -f .env ] && ok ".env" || miss ".env: not written yet"
  exit 1
fi
[ ${#MISSING[@]} -eq 0 ] || fail "Still missing: ${MISSING[*]}. Install them and rerun ./setup.sh."

# ---- .env: the model and its key
bold "Model"
set_env() {  # set_env KEY VALUE: replace the KEY= line in .env (values are never printed)
  KEY="$1" VALUE="$2" uv run --no-project --quiet python - <<'PY'
import os, re
k, v = os.environ["KEY"], os.environ["VALUE"]
text = open(".env").read()
line = f"{k}={v}"
text = re.sub(rf"^{re.escape(k)}=.*$", lambda _: line, text, count=1, flags=re.M) if re.search(rf"^{re.escape(k)}=", text, re.M) else text + f"\n{line}\n"
open(".env", "w").write(text)
PY
}
has_key() { grep -Eq '^(ANTHROPIC|SILICONFLOW|OPENROUTER|OPENAI)_API_KEY=.+' .env 2>/dev/null; }
[ -f .env ] || { cp .env.example .env; ok "wrote .env from .env.example"; }
if has_key; then
  ok ".env has a model key"
elif interactive; then
  cat <<'MENU'
  Which model provider will you use?
    1) Anthropic (Claude)        3) SiliconFlow (Qwen, low cost)
    2) OpenRouter (many models)  4) Something else: I'll edit .env myself
MENU
  printf '  [1-4] '; read -r choice
  case "$choice" in
    1) var=ANTHROPIC_API_KEY; llm=anthropic; gen=claude-opus-5-5; cls=claude-haiku-5-5 ;;
    2) var=OPENROUTER_API_KEY; llm=litellm; gen=openrouter/anthropic/claude-opus-5.5; cls=openrouter/anthropic/claude-haiku-5.5 ;;
    3) var=SILICONFLOW_API_KEY; llm=litellm; gen=siliconflow/Qwen/Qwen3.5-35B-A3B; cls=$gen ;;
    *) var='' ;;
  esac
  if [ -n "$var" ]; then
    printf '  Paste your %s (input hidden): ' "$var"; read -rs key; echo
    [ -n "$key" ] || fail "no key entered; rerun ./setup.sh when you have one"
    set_env "$var" "$key"; set_env YACHAT_LLM "$llm"; set_env YACHAT_MODEL_GENERATE "$gen"; set_env YACHAT_MODEL_CLASSIFY "$cls"
    ok "saved $var and the model settings in .env"
  else
    miss "edit .env: set YACHAT_LLM, your provider's *_API_KEY and YACHAT_MODEL_GENERATE / YACHAT_MODEL_CLASSIFY"
  fi
else
  miss "no model key in .env yet: edit it, or rerun ./setup.sh in a terminal to be asked"
fi

# ---- dependencies
bold "Dependencies"
uv sync --all-extras --quiet && ok "Python packages (uv sync)"
(cd frontend && npm ci --silent --no-audit --no-fund && npm run build --silent >/dev/null) && ok "console built (frontend/dist)"
if [ -n "$DOCKER" ] && docker info >/dev/null 2>&1; then
  docker compose --profile app build --quiet && ok "Docker image yachat:local"
fi

bold "Ready."
cat <<'NEXT'
  scripts/try.sh            the console with a sample bot, everything in memory
  scripts/local.sh up       the whole stack in Docker (API, console, Redis, Postgres)
  ./run.sh                  the console with live reload, for working on yachat itself
NEXT
has_key || echo "  (add a model key to .env first)"
