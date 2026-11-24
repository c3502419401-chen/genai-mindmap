# syntax=docker/dockerfile:1
#
# genai-mindmap 前端镜像：Vue 3 + Vite 构建 → nginx 托管静态产物 + /api 反代
#
# 两段式：builder 里跑 `npm run build`，最终镜像只留 dist 与 nginx 配置。
# 构建上下文同样是**仓库根目录**（compose 里 context: .），因为 .dockerignore
# 只在根的上下文里生效，而且这样两个镜像共用一份忽略规则。
#
# 构建：
#   docker build -f docker/frontend.Dockerfile -t genai-mindmap-frontend .
# 或者：docker compose build frontend
#
# Node 24 与 CI（.github/workflows/ci.yml 的 node-version: "24"）一致。

# ---------------------------------------------------------------- 构建段
FROM node:24-slim AS build

WORKDIR /app

# 先只拷清单：package*.json 没变时这一层命中缓存，改源码不会重装依赖。
# 用 npm ci 而不是 npm install：lockfile 是权威，install 会顺手升级。
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# 再拷源码。tsconfig.json 的 include 是 ["src", "tests", "vite.config.ts"]，
# 所以 tests/ 也要进来——少拷它的话 `vue-tsc --noEmit` 检查的文件集会变，
# 镜像里"构建通过"就和 CI 里"构建通过"不是同一件事了。
COPY frontend/tsconfig.json frontend/vite.config.ts frontend/index.html ./
COPY frontend/src ./src
COPY frontend/tests ./tests

# `npm run build` = `vue-tsc --noEmit && vite build`，与 CI、与 AGENTS.md 里
# "build 交给 CI" 是同一条命令。类型错误会让构建失败——这是故意的：宁可
# 镜像盖不出来，也不要盖出一个类型不对的产物。
#
# 不需要 ../backend/openapi.json：`gen:api` 不在 build 链上，
# frontend/src/api/schema.d.ts 已经提交在仓库里。
#
# 不传 VITE_API_BASE：frontend/src/api/client.ts:45 缺省就是 '/api'，
# 与静态资源同源，正是这里要的形态。
RUN npm run build

# ---------------------------------------------------------------- 运行段
# alpine 版 nginx 约 20MB。nginx 主版本钉住、alpine 浮动，兼顾可复现与安全更新。
FROM nginx:1.27-alpine

# 站点配置整份替换掉官方镜像的 default.conf：默认那份监听 80、root 指向
# /usr/share/nginx/html，但没有 /api 反代，也没有 SSE 需要的关缓冲。
# 容器版 nginx **没有任何 Basic Auth**：鉴权在应用层（门禁要求有身份 + 密钥与
# 地址绑定 + probe 拒内网），全站口令反而会挡住游客的免费试用。
# 理由见 docs/DOCKER.md 第 11 节。
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

# 容器里的 nginx 必须以非 daemon 方式前台跑，否则容器一启动就退出。
# 官方镜像的 CMD 已经是 `nginx -g 'daemon off;'`，这里写出来是为了让
# "为什么能起"这件事在文件里可见，不依赖基础镜像的默认值。
EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
