#!/usr/bin/env bash
# 在线备份 SQLite 数据文件。由 /etc/cron.d/genai-mindmap-backup 每天调用，
# 也可以手动跑：sudo /usr/local/bin/genai-mindmap-backup
#
# 用 sqlite 的 .backup 命令而不是 cp：服务在跑的时候直接复制文件，
# 有可能拿到"页写了一半"的副本（WAL / 未落盘页），恢复时才发现库损坏。
set -euo pipefail

# cron 不继承 systemd 的 EnvironmentFile。不自己读一遍的话，运维把 MINDMAP_DB_PATH
# 改到别处之后，这里仍然用下面的默认值——而下面"文件不存在"的分支在没配过路径时
# 只留一行提示就退出，结果是每天"成功"地零备份，直到要恢复才发现。`set -a` 让
# source 进来的变量能传给 sqlite3 等子进程。
if [[ -r /etc/genai-mindmap/env ]]; then
  set -a
  # shellcheck disable=SC1091
  . /etc/genai-mindmap/env
  set +a
fi

DB="${MINDMAP_DB_PATH:-/var/lib/genai-mindmap/mindmaps.db}"
DEST="${MINDMAP_BACKUP_DIR:-/var/backups/genai-mindmap}"
KEEP_DAYS="${MINDMAP_BACKUP_KEEP_DAYS:-14}"

if [[ ! -f "$DB" ]]; then
  # 区分两件事：显式配了路径却找不到文件 = 配错了，要报错让 cron 日志留下痕迹；
  # 没配过（用默认值）且文件不存在 = 服务还没写过库，跳过即可。
  if [[ -n "${MINDMAP_DB_PATH:-}" ]]; then
    echo "$(date '+%F %T') MINDMAP_DB_PATH 指向的文件不存在：$DB" >&2
    exit 1
  fi
  echo "$(date '+%F %T') 没有数据库文件：$DB，跳过"
  exit 0
fi

mkdir -p "$DEST"
STAMP="$(date '+%Y%m%d-%H%M%S')"
OUT="$DEST/mindmaps-$STAMP.db"

sqlite3 "$DB" ".backup '$OUT'"
gzip -f "$OUT"

# 保留最近 N 天；按修改时间删，避免依赖文件名里的时间戳格式
find "$DEST" -name 'mindmaps-*.db.gz' -type f -mtime "+$KEEP_DAYS" -delete

echo "$(date '+%F %T') 已备份 ${DB} -> ${OUT}.gz（保留 ${KEEP_DAYS} 天）"
