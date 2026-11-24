#!/usr/bin/env bash
# 在 ECS 上装一个 self-hosted runner，用来把"打 tag"变成自动部署。
#
#   sudo bash deploy/install-runner.sh --token <注册 token>
#
# token 在仓库页面拿：Settings → Actions → Runners → New self-hosted runner，
# 或者命令行：
#   gh api -X POST repos/RPeGio/genai-mindmap/actions/runners/registration-token --jq .token
# token 一小时后过期，装完就没用了。
#
# 装完之后这台机器**不需要任何入方向端口**：runner 主动连 github.com 出站取任务。
set -euo pipefail

RUNNER_USER="ghrunner"
RUNNER_HOME="/home/$RUNNER_USER"
RUNNER_DIR="$RUNNER_HOME/actions-runner"
REPO_URL="https://github.com/RPeGio/genai-mindmap"
LABELS="genai-ecs"
TOKEN=""
CD_APPLY="/usr/local/sbin/genai-cd-apply"

die() { printf '出错：%s\n' "$1" >&2; exit 1; }
say() { printf '\n== %s\n' "$1"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)  TOKEN="${2:-}"; shift 2 ;;
    --labels) LABELS="${2:-}"; shift 2 ;;
    --repo)   REPO_URL="${2:-}"; shift 2 ;;
    *) die "未知参数 $1" ;;
  esac
done

[[ "$(id -u)" == "0" ]] || die "请用 root 运行（sudo）"
[[ -n "$TOKEN" ]] || die "缺少 --token（去仓库 Settings → Actions → Runners 拿）"
[[ -x "$CD_APPLY" ]] || die "$CD_APPLY 不存在。先跑一次 deploy/install.sh，它会把 cd-apply.sh 装到这里"

say "准备用户 $RUNNER_USER"
if ! id "$RUNNER_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$RUNNER_USER"
fi

say "下载 runner"
command -v curl >/dev/null || die "缺 curl"
api="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest)" \
  || die "拿不到 runner 版本（出站到 api.github.com 不通？）"
version="$(printf '%s' "$api" | sed -n 's/.*"tag_name":[[:space:]]*"v\([^"]*\)".*/\1/p' | head -n 1)"
[[ -n "$version" ]] || die "解析 runner 版本失败"
echo "runner 版本：$version"

mkdir -p "$RUNNER_DIR"
chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_HOME"
tar_name="actions-runner-linux-x64-${version}.tar.gz"
direct="https://github.com/actions/runner/releases/download/v${version}/${tar_name}"

if [[ -x "$RUNNER_DIR/config.sh" ]]; then
  # 上一次已经解过包（哪怕注册失败），不用再下 200MB
  say "runner 已经在 $RUNNER_DIR，跳过下载"
else
  # github.com 首页能连，并不代表 release 的下载地址能连：下载会 302 到
  # release-assets.githubusercontent.com（旧版是 objects.githubusercontent.com），
  # 那台域名在部分网络里是"连得上但立刻被断开"，curl 报 (52) Empty reply from server。
  # 所以这里依次尝试：直连 → 几个常用 GitHub 加速前缀。
  say "下载 runner ${version}"
  downloaded=0
  for prefix in "" "https://ghfast.top/" "https://gh-proxy.com/" "https://ghproxy.net/"; do
    url="${prefix}${direct}"
    echo "尝试：${url}"
    if runuser -u "$RUNNER_USER" -- curl -fL --http1.1 \
        --connect-timeout 15 --max-time 900 --retry 3 --retry-delay 3 \
        "$url" -o "$RUNNER_DIR/runner.tar.gz"; then
      # 镜像偶尔会"下到一半断开"，大小对不上就当失败
      size="$(stat -c %s "$RUNNER_DIR/runner.tar.gz" 2>/dev/null || echo 0)"
      if [[ "$size" -gt 100000000 ]]; then
        downloaded=1
        break
      fi
      echo "  只下到 $((size / 1024 / 1024))MB，不完整，换下一个源"
    fi
    echo "  这个源不行，换下一个"
    rm -f "$RUNNER_DIR/runner.tar.gz"
  done
  [[ "$downloaded" == "1" ]] || die "所有下载源都失败。先确认这台机器能不能连 release 域名：
  curl -sSI --max-time 8 https://release-assets.githubusercontent.com | head -n 1
  curl -sSI --max-time 8 https://objects.githubusercontent.com | head -n 1
两条都报 (52)/(56)/超时的话，就是下载域名被挡；把能用的镜像加到上面的列表里再跑。"
  runuser -u "$RUNNER_USER" -- tar -xzf "$RUNNER_DIR/runner.tar.gz" -C "$RUNNER_DIR"
  rm -f "$RUNNER_DIR/runner.tar.gz"
fi

say "注册到仓库（标签 $LABELS）"
# 重跑时 runner 目录里可能已有 .runner/.credentials，config.sh 会直接报
# "Cannot configure the runner because it is already configured"，先清掉再注册。
if [[ -f "$RUNNER_DIR/.runner" ]]; then
  echo "检测到旧的注册信息，先移除"
  ( cd "$RUNNER_DIR" && runuser -u "$RUNNER_USER" -- ./config.sh remove --token "$TOKEN" ) \
    || rm -f "$RUNNER_DIR/.runner" "$RUNNER_DIR/.credentials" "$RUNNER_DIR/.credentials_rsaparams"
fi

# 必须在 runner 目录里跑：config.sh 会用相对路径找 ./bin
( cd "$RUNNER_DIR" && runuser -u "$RUNNER_USER" -- ./config.sh \
    --url "$REPO_URL" \
    --token "$TOKEN" \
    --name "$(hostname)-genai" \
    --labels "$LABELS" \
    --work "$RUNNER_HOME/_work" \
    --unattended --replace )

say "装成系统服务"
( cd "$RUNNER_DIR" && ./svc.sh install "$RUNNER_USER" >/dev/null )
( cd "$RUNNER_DIR" && ./svc.sh start )

say "开放唯一一条 sudo 权限"
cat > /etc/sudoers.d/genai-cd <<EOF
# 让 runner 用户能触发部署，但只能执行这一个 root 拥有的脚本
$RUNNER_USER ALL=(root) NOPASSWD: $CD_APPLY
EOF
chmod 0440 /etc/sudoers.d/genai-cd
visudo -cf /etc/sudoers.d/genai-cd >/dev/null || die "sudoers 语法不对，已保留文件供检查"

cat <<EOF

装好了。接下来：
  1. 回仓库 Settings → Actions → Runners，确认有一台 idle 的 genai-ecs
  2. 打一个 tag 试试：git tag v0.5.0 && git push origin v0.5.0
  3. 看到 Actions 的 Deploy 跑绿之后，把安全组的 22 端口关掉，
     并删掉 root 的 authorized_keys 里的部署公钥（Workbench 不受影响）

排障：
  journalctl -u 'actions.runner.*' -f
  sudo -u $RUNNER_USER $RUNNER_DIR/run.sh --once      # 前台跑一轮看日志
EOF
