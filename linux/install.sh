#!/usr/bin/env bash
# Upgrade the per-user installation and preserve its active and login-enabled states.
set -Eeuo pipefail
umask 077
src=$(cd -- "$(dirname -- "$0")" && pwd)
bin="$HOME/.local/bin"
lib="$HOME/.local/lib/codex-ny"
cfg="$HOME/.config/codex-ny"
units="$HOME/.config/systemd/user"
unit_name=codex-ny.service
engine=/usr/lib/chatgpt/resources/codex
script="$HOME/.local/share/io.github.clash-verge-rev.clash-verge-rev/profiles/Script.js"

for cmd in curl ip ss systemctl flock; do command -v "$cmd" >/dev/null || { echo "缺少 $cmd" >&2; exit 1; }; done
install -d -m 700 "$cfg"
exec 9>"$cfg/install.lock"
flock -x 9
(
  cd "$src"
  /usr/bin/python3 -B -c 'import clash_route as c; c.node(); c.engines()'
)
systemctl --user show-environment >/dev/null
if [[ -e "$bin/codex-ny" || -L "$bin/codex-ny" ]]; then
  [[ $(readlink "$bin/codex-ny" || true) == "$lib/codex-ny" ]] || { echo "$bin/codex-ny 已由其他程序使用。" >&2; exit 1; }
fi
if [[ -e "$units/$unit_name" && ! -f "$lib/codex-ny" ]]; then
  echo "已有同名 systemd 单元，但未找到原版 codex-ny。" >&2; exit 1
fi
if [[ -L "$bin/codex-ny" ]] && cmp -s "$src/codex-ny" "$lib/codex-ny" &&
    cmp -s "$src/clash_route.py" "$lib/clash_route.py" && cmp -s "$src/verify.py" "$lib/verify.py" &&
    cmp -s "$src/$unit_name" "$units/$unit_name"; then
  if systemctl --user is-active --quiet "$unit_name"; then
    if ! (cd "$src" && /usr/bin/python3 -B -c 'import sys,clash_route as c; sys.exit(not c.current_node_matches())'); then
      systemctl --user restart "$unit_name"
      echo "Hysteria 2 已载入当前节点配置。"
    fi
  fi
  echo "当前已安装相同的 Hysteria 2 版本。"; exit 0
fi
install -d "$lib" "$bin" "$units"
install -d -m 700 "$cfg/backups"
backup=$(mktemp -d "$cfg/backups/upgrade-$(date +%Y%m%d-%H%M%S)-XXXXXX")
files=(codex-ny verify.py clash_route.py)
for name in "${files[@]}"; do
  if [[ -f "$lib/$name" ]]; then cp -p "$lib/$name" "$backup/$name"; fi
done
if [[ -f "$units/$unit_name" ]]; then cp -p "$units/$unit_name" "$backup/$unit_name"; fi
was_active=$(systemctl --user is-active "$unit_name" || true)
was_enabled=$(systemctl --user is-enabled "$unit_name" 2>/dev/null || true)
printf '%s\n' "$was_active" >"$backup/active"
printf '%s\n' "$was_enabled" >"$backup/enabled"
was_on=0
case "$was_active" in active|activating|reloading) was_on=1;; esac
legacy=0
had_marker=0
had_rules=0
if [[ -f "$lib/codex-ny" ]] && grep -q 'Codex-NY-Ubuntu' "$lib/codex-ny"; then
  legacy=1
  if [[ -f "$script" ]] && grep -Fq '// BEGIN codex-ny managed rules' "$script"; then had_marker=1; fi
  if /usr/bin/python3 -B -c 'import runpy,sys; sys.exit(not runpy.run_path(sys.argv[1])["runtime_active"]())' "$lib/codex-ny" 2>/dev/null; then had_rules=1; fi
fi
had_ny_link=0; [[ ! -L "$bin/codex-ny" ]] || had_ny_link=1
made_cli_link=0
published=0
restore_enablement() {
  case "$was_enabled" in
    enabled) systemctl --user enable "$unit_name" ;;
    enabled-runtime) systemctl --user enable --runtime "$unit_name" ;;
    *) systemctl --user disable "$unit_name" ;;
  esac
}
rollback() {
  local rc=$? name restore_rc=0
  trap - EXIT HUP INT TERM
  if (( rc == 0 )); then return; fi
  set +e
  echo "升级失败，正在还原原版本。备份位于 $backup" >&2
  if (( published )); then systemctl --user stop "$unit_name" 2>/dev/null; fi
  for name in "${files[@]}"; do
    if [[ -f "$backup/$name" ]]; then cp -pf "$backup/$name" "$lib/$name" || restore_rc=1; else rm -f -- "$lib/$name"; fi
  done
  if [[ -f "$backup/$unit_name" ]]; then
    cp -pf "$backup/$unit_name" "$units/$unit_name" || restore_rc=1
  else
    systemctl --user disable "$unit_name" 2>/dev/null
    rm -f -- "$units/$unit_name"
  fi
  if (( ! had_ny_link )); then rm -f -- "$bin/codex-ny"; fi
  if (( made_cli_link )); then rm -f -- "$bin/codex"; fi
  systemctl --user daemon-reload || restore_rc=1
  if [[ -f "$backup/$unit_name" ]]; then
    if (( was_on )); then systemctl --user start "$unit_name" || restore_rc=1; fi
    restore_enablement || restore_rc=1
  fi
  if (( legacy && (had_marker || had_rules) )); then
    /usr/bin/python3 -B - "$lib/codex-ny" "$had_marker" "$had_rules" <<'PY' || restore_rc=1
import runpy, sys
old = runpy.run_path(sys.argv[1])
old["set_extension"](sys.argv[2] == "1")
if old["runtime_active"]() != (sys.argv[3] == "1"):
    old["apply"](sys.argv[3] == "1")
PY
  fi
  if (( restore_rc )); then echo "原版本已备份，部分还原步骤失败，请检查 codex-ny status。" >&2; fi
  exit "$rc"
}
trap rollback EXIT
trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM

if (( legacy && (was_on || had_marker || had_rules) )); then
  "$lib/codex-ny" stop
elif (( was_on )); then
  systemctl --user stop "$unit_name"
elif [[ -x "$lib/codex-ny" && -f "$cfg/clash-restore.json" ]]; then
  "$lib/codex-ny" _recover
fi
published=1
for name in "${files[@]}" "$unit_name"; do
  dest="$lib/$name"; mode=644
  if [[ "$name" == "$unit_name" ]]; then dest="$units/$name"; fi
  if [[ "$name" == codex-ny ]]; then mode=755; fi
  install -m "$mode" "$src/$name" "$dest.new.$$"
  mv -fT "$dest.new.$$" "$dest"
done
ln -sfn "$lib/codex-ny" "$bin/codex-ny"
if [[ -x "$engine" && ! -e "$bin/codex" && ! -L "$bin/codex" ]]; then
  ln -s "$engine" "$bin/codex"; made_cli_link=1
fi
systemctl --user daemon-reload
if (( was_on )); then systemctl --user start "$unit_name"; fi
restore_enablement
trap - EXIT HUP INT TERM
echo "Hysteria 2 版本已安装。可使用 codex-ny start、stop、status、test。"
echo "升级备份 $backup"
