#!/usr/bin/env bash
#
# Check this machine can run the platform, before you download anything.
#
# On macOS and Windows, Docker runs in a VM and reports that VM's memory and CPU rather
# than the machine's. Those are the figures that decide whether the driver starts, and a
# 4GB VM fails after the download finishes, with an out-of-memory kill that says nothing
# about which setting to change.
#
# Usage: ./preflight.sh
# Exits non-zero if a required check fails. Warnings do not.

set -uo pipefail

MIN_MEM_GB=6        # ~3GB measured idle; rest is headroom for queries
REC_MEM_GB=8
MIN_CPU=4
MIN_DISK_GB=15      # images unpack to about 10GB

pass=0; warn=0; fail=0
ok()   { printf '  PASS  %s\n' "$1"; pass=$((pass+1)); }
wn()   { printf '  WARN  %s\n' "$1"; warn=$((warn+1)); }
no()   { printf '  FAIL  %s\n' "$1"; fail=$((fail+1)); }
hint() { printf '        -> %s\n' "$1"; }

printf '%s\n' "Zetaris platform -- preflight"
printf '%s\n' "============================="
printf '\n'

# --- docker itself ------------------------------------------------------------------------
# What to type, for the system you are on. The package names and what they give you differ
# more than expected: Debian has no compose plugin at all, and on RHEL the package called
# docker is podman.
how_to_install_docker() {
    local id=""
    [ -r /etc/os-release ] && id=$(. /etc/os-release 2>/dev/null; echo "${ID:-}")
    case "$(uname -s):${id}" in
        Darwin:*)
            hint "brew install --cask docker     (then open Docker Desktop once)"
            hint "give Docker 8GB of memory in Settings > Resources"
            ;;
        Linux:ubuntu)
            hint "sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2"
            hint "sudo usermod -aG docker \$USER && newgrp docker"
            ;;
        Linux:debian)
            hint "sudo apt-get update && sudo apt-get install -y docker.io"
            hint "Debian has no compose package. Install the plugin as well:"
            hint "  sudo mkdir -p /usr/local/lib/docker/cli-plugins"
            hint "  sudo curl -sL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-\$(uname -m) \\"
            hint "       -o /usr/local/lib/docker/cli-plugins/docker-compose"
            hint "  sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose"
            hint "sudo usermod -aG docker \$USER && newgrp docker"
            ;;
        Linux:amzn)
            hint "sudo dnf install -y docker && sudo systemctl enable --now docker"
            hint "sudo usermod -aG docker \$USER && newgrp docker"
            ;;
        Linux:rhel|Linux:centos|Linux:rocky|Linux:almalinux)
            hint "NOT \`dnf install docker\` -- on this system that is podman, which will not work."
            hint "sudo dnf install -y dnf-plugins-core"
            hint "sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo"
            hint "sudo dnf install -y --allowerasing docker-ce docker-ce-cli containerd.io docker-compose-plugin"
            hint "sudo systemctl enable --now docker"
            ;;
        Linux:fedora)
            hint "sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin"
            hint "sudo systemctl enable --now docker"
            ;;
        *)
            hint "install Docker Engine 25+ and the compose plugin: https://docs.docker.com/engine/install/"
            ;;
    esac
}

if ! command -v docker >/dev/null 2>&1; then
    no "docker is not installed"
    how_to_install_docker
    printf '\nCannot continue without Docker.\n'
    exit 1
fi

# A docker command that is really podman answers `docker --version` and then fails on
# everything else. RHEL ships that shim as "docker".
if docker --version 2>/dev/null | grep -qi podman; then
    no "this is podman, not docker -- the compose deployment needs a docker daemon"
    hint "sudo dnf remove -y podman-docker"
    how_to_install_docker
    printf '\nCannot continue: podman is not a substitute here.\n'
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    no "the docker daemon is not reachable"
    hint "start Docker Desktop, or on linux: sudo systemctl start docker"
    hint "if it is running, your user may not be in the docker group:"
    hint "  sudo usermod -aG docker \$USER && newgrp docker"
    printf '\nCannot continue without a running daemon.\n'
    exit 1
fi

dver=$(docker version --format '{{.Server.Version}}' 2>/dev/null)
dmaj=${dver%%.*}
if [ -n "${dmaj:-}" ] && [ "$dmaj" -ge 25 ] 2>/dev/null; then
    ok "docker ${dver}"
else
    wn "docker ${dver:-unknown} -- 25.0 or newer is recommended"
fi

if docker compose version >/dev/null 2>&1; then
    ok "compose plugin $(docker compose version --short 2>/dev/null)"
else
    no "the docker compose plugin is missing"
    how_to_install_docker
fi

# --- architecture -------------------------------------------------------------------------
# The images are linux/amd64, so Apple Silicon runs them under emulation. Rosetta costs
# roughly 1.5x, QEMU nearer 10x. Both work, so warn rather than fail.
darch=$(docker info --format '{{.Architecture}}' 2>/dev/null)
case "$darch" in
    aarch64|arm64)
        wn "arm64 host -- the platform images are amd64 and will run under emulation"
        hint "Docker Desktop: Settings > General > 'Use Rosetta for x86_64/amd64 emulation'"
        hint "Colima: colima start --vm-type=vz --vz-rosetta"
        hint "without Rosetta expect the platform to be several times slower"
        ;;
    x86_64|amd64) ok "architecture ${darch}" ;;
    *)            wn "unrecognised architecture '${darch:-unknown}'" ;;
esac

# --- memory -------------------------------------------------------------------------------
# On macOS and Windows this is the VM's allocation, not the machine's RAM.
memb=$(docker info --format '{{.MemTotal}}' 2>/dev/null)
if [ -n "${memb:-}" ] && [ "$memb" -gt 0 ] 2>/dev/null; then
    memg=$(( memb / 1073741824 ))
    if   [ "$memg" -ge "$REC_MEM_GB" ]; then ok "memory available to docker: ${memg}GB"
    elif [ "$memg" -ge "$MIN_MEM_GB" ]; then wn "memory available to docker: ${memg}GB (${REC_MEM_GB}GB recommended)"
    else
        no "memory available to docker: ${memg}GB -- at least ${MIN_MEM_GB}GB is needed"
        hint "Docker Desktop: Settings > Resources > Memory, raise it to ${REC_MEM_GB}GB"
        hint "Colima: colima stop && colima start --memory ${REC_MEM_GB}"
        hint "on linux this is the host's RAM; the stack will not fit on this machine"
    fi
else
    wn "could not read the memory available to docker"
fi

# --- cpu ----------------------------------------------------------------------------------
cpus=$(docker info --format '{{.NCPU}}' 2>/dev/null)
if [ -n "${cpus:-}" ] && [ "$cpus" -gt 0 ] 2>/dev/null; then
    if [ "$cpus" -ge "$MIN_CPU" ]; then ok "cpus available to docker: ${cpus}"
    else
        wn "cpus available to docker: ${cpus} (${MIN_CPU} recommended)"
        hint "it will run, but queries will be noticeably slow"
    fi
else
    wn "could not read the cpu count"
fi

# --- disk ---------------------------------------------------------------------------------
# Measured where the daemon stores images. On macOS and Windows that is inside the VM, so
# free space in your home directory is not the relevant number.
root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)
[ -d "${root:-}" ] || root=/
availg=$(( $(df -Pk "$root" 2>/dev/null | awk 'NR==2 {print $4}') / 1048576 ))
if [ "$availg" -ge "$MIN_DISK_GB" ]; then
    ok "disk free where docker stores images: ${availg}GB"
else
    no "disk free where docker stores images: ${availg}GB -- at least ${MIN_DISK_GB}GB is needed"
    hint "the images unpack to about 10GB; ${root} is the filesystem that has to hold them"
    hint "reclaim some with: docker system prune -a"
fi

# --- leftover volumes ---------------------------------------------------------------------
# docker-compose.yml pins `name: zetaris`, so a second install on the same machine adopts the
# volumes of the first. That is usually what you want -- until the earlier one was created
# with a different POSTGRES_PASSWORD, because Postgres only applies that password when it
# initialises an empty data directory. The stack then fails deep inside the query engine with
# a Java stack trace ending "password authentication failed for user", which says nothing
# about volumes.
if docker volume ls --format '{{.Name}}' 2>/dev/null | grep -q '^zetaris_pgdata$'; then
    wn "an earlier install's data is still on this machine (volume zetaris_pgdata)"
    hint "it will be reused, and if it was created with a different password the"
    hint "  query engine fails with 'password authentication failed for user'"
    hint "to start clean instead:  docker compose down -v"
fi

# --- registry -----------------------------------------------------------------------------
# Checked here rather than left to the pull: `pull access denied` reads as a missing image
# rather than a missing login, and by then several GB may already have downloaded.
REGISTRY=zetarishackathon.azurecr.io

# Both locations: `sudo docker login` writes root's config, and whether sudo keeps HOME
# differs by distribution, so one path alone reports a login that happened as missing.
signed_in=0
for cfg in "${DOCKER_CONFIG:-$HOME/.docker}/config.json" /root/.docker/config.json; do
    grep -qs "$REGISTRY" "$cfg" 2>/dev/null && signed_in=1
done
if [ "$signed_in" -eq 1 ]; then
    ok "signed in to ${REGISTRY}"
else
    no "not signed in to ${REGISTRY}"
    hint "paste the sign-in command from the Zetaris Cloud download page"
    hint "without it the pull fails several minutes in, with an error about the image"
fi

# --- summary ------------------------------------------------------------------------------
printf '\n'
if [ "$fail" -gt 0 ]; then
    printf '%d check(s) failed, %d warning(s).\n' "$fail" "$warn"
    printf 'Fix the failures above, then run ./preflight.sh again before downloading.\n'
    exit 1
fi
if [ "$warn" -gt 0 ]; then
    printf 'All required checks passed, with %d warning(s) -- the stack will run.\n' "$warn"
else
    printf 'All checks passed.\n'
fi
exit 0
