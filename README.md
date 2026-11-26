# Clash Traffic Routing

让这台电脑上的 **Codex 模型请求通过自己的 Hysteria 2 VPS 出口**，同时让浏览器、Git 和其他应用继续使用原有的 Clash 线路。项目包含 macOS 菜单栏开关「Codex 纽约线路」、Ubuntu 用户服务 `codex-ny` 和 VPS 安装脚本。“纽约”是开关的名称，实际出口取决于你部署 VPS 的位置。

Codex 继续在本机运行和读写项目文件。Clash Verge Rev 的 Mihomo 内核按 **Codex 可执行文件的完整路径** 分流。浏览器即使访问相同的 `chatgpt.com`，也会按普通应用的规则走。开关自身的出口探测也会经过 VPS。

| 操作 | Codex 引擎的新连接 | 其他应用 |
| --- | --- | --- |
| 开启 | 本机 Clash → Hysteria 2（UDP 443）→ VPS → OpenAI | 沿用 Clash 的订阅规则、全局所选节点或直连模式 |
| 关闭 | 回到原有 Clash 线路 | 继续使用 Clash |

## 使用条件

- **VPS**：Linux x86_64、systemd、Python 3.8+、curl、OpenSSL、iproute2，以及 root 安装权限。云安全组和主机防火墙需允许客户端访问 **UDP 443**。
- **Mac**：macOS 14+、Xcode Command Line Tools、已安装带内置 Codex 引擎的 ChatGPT 桌面应用。当前匹配的引擎路径见 [macOS 分流模块](macos/clash_route.py) 中的 `ENGINE`。
- **Ubuntu**：以 Ubuntu 24.04 为目标，使用 systemd 用户服务、Python 3、PyYAML、iproute2、curl 和已登录的原生 Codex。使用 Debian 系软件包时可用 `sudo apt install python3-yaml iproute2 curl` 补充依赖。
- 两个平台的客户端都需要先启动 **Clash Verge Rev** 并启用 **TUN**。分流依赖 Mihomo 的 Unix 控制套接字和进程识别，项目在 Mihomo 1.19 系列上开发。

在需要的机器上获取源码。只有 VPS 需要执行服务端安装步骤。

```sh
git clone https://github.com/WekPedge/Clash-Traffic-Routing.git
cd Clash-Traffic-Routing
```

## 1. 在 VPS 上部署 Hysteria 2

将下方的示例地址替换成自己的 VPS 公网 IPv4。在仓库根目录运行：

```sh
VPS_IP=203.0.113.10
sudo python3 server/provision.py install "$VPS_IP"
systemctl is-active codex-ny-hy2.service
```

安装器获取 Hysteria 2 `app/v2.14.0` 的 Linux amd64 二进制，核对固定的 SHA-256，生成随机密码和自签证书，并启动低权限 systemd 服务。Hysteria 2 服务只转发发往所配置的 OpenAI、静态资源和探测域名的 TCP 443 连接。完整允许列表位于 [服务端安装器](server/provision.py)。

客户端文件生成在 `/etc/codex-ny-hy2/client-profile.yaml`，仅 root 可读。文件包含节点密码和证书指纹。对于本项目已有的 VPS 部署，可在保留原凭据的情况下重新导出：

```sh
sudo python3 server/provision.py export "$VPS_IP"
```

如果 VPS 的公网入口和实际出站 IP 不同，在 `install` 或 `export` 命令末尾附加 `--expected-egress-ip 实际公网IPv4`。客户端用这个地址核对出口。

## 2. 将节点配置装到每台客户端

在 Mac 或 Ubuntu 上替换 `VPS_SSH` 为自己的 SSH 主机别名或登录地址。以下示例使用有权读取服务配置的 VPS 账号。普通管理账号可把远端命令改为 `sudo -n cat /etc/codex-ny-hy2/client-profile.yaml`，前提是该账号已有相应权限。

```sh
VPS_SSH=root@203.0.113.10
(
  set -eu
  umask 077
  case "$(uname -s)" in
    Darwin) dir="$HOME/.config/codex-ny-hy2" ;;
    Linux)  dir="$HOME/.config/codex-ny" ;;
    *) exit 1 ;;
  esac
  mkdir -p "$dir"
  tmp=$(mktemp "$dir/.node.XXXXXXXX")
  trap 'rm -f "$tmp"' EXIT
  ssh "$VPS_SSH" 'cat /etc/codex-ny-hy2/client-profile.yaml' > "$tmp"
  test -s "$tmp"
  mv "$tmp" "$dir/ny-hy2-profile.yaml"
)
```

请保留服务端导出的 JSON 格式（兼容 YAML）和文件权限 `0600`。客户端默认选择物理出口网卡。多网卡环境可在该文件的代理条目中设置 `interface-name`，填写本机实际能到达 VPS 的网卡名。

## 3A. macOS 菜单栏开关

```sh
sh macos/build.sh
open "macos/build/Codex 纽约线路.app"
```

也可以将生成的应用移动到「应用程序」后双击。菜单栏显示 **Codex NY** 时，在原来的本地 Codex 会话中继续工作即可。点「测试纽约链路」核对出口，点「测量到纽约 VPS 的延迟」测量本机到 VPS 的 ICMP 往返时间。点「关闭纽约线路，恢复 Clash」退出。

开启期间，在 Clash Verge 里选择新的规则、全局或直连模式，其他应用的新连接会跟随该选择，退出时也会保留最后选定的模式。切换会重建 Codex 连接，建议等正在生成的回复结束后再操作。

## 3B. Ubuntu 用户服务

以运行 Codex 的普通用户执行：

```sh
bash linux/install.sh
~/.local/bin/codex-ny start
~/.local/bin/codex-ny status
~/.local/bin/codex-ny test
~/.local/bin/codex-ny stop
```

`start` 会开启分流并启用登录自启，`stop` 会恢复原 Clash 线路并取消自启。升级旧版时，安装器会先备份并保留服务此前的运行、自启状态。Ubuntu 版按启动时的 Clash 模式保留其他应用的线路；要改变基础模式，可以先停止开关、在 Clash 里选好模式，再启动。

默认识别 `/usr/lib/chatgpt/resources/codex` 和 `~/.local/bin/codex` 指向的原生引擎。其他安装位置可以写入 `~/.config/codex-ny/engine-paths`，每行一个原生 ELF 可执行文件的绝对路径。日志可用 `journalctl --user -u codex-ny.service -n 40` 查看。

## 验证与排查

- **验证真实模型请求**：Ubuntu 的 `codex-ny test` 会发送一条简短的真实请求，按进程套接字核对 Clash 代理链，并比较 Codex 和普通应用访问同一 `chatgpt.com` 的出口。该测试使用当前 Codex 登录账号和额度。macOS 的菜单自检检查进程规则和节点出口；要验证真实对话，可发送一条消息，在 Clash「连接」中查看 Codex 引擎和 `CodexNY-Hysteria2` 代理链。
- **公司仓库或内网站点**：查看浏览器连接命中的规则。规则模式沿用原订阅的直连规则。全局模式下，普通应用继续使用原先选中的全局节点。
- **连接 VPS 超时**：核对 UDP 443、物理网卡和 VPS 服务状态。更换证书或密码后重新导出客户端文件并重启开关。节点配置留在用户配置目录，不要提交到 Git。
- **macOS 模式切换回归测试**：在仓库根目录执行 `(cd macos && python3 -B -m unittest test_mode)`。
