# 前端 + 后端一起打进一个镜像。
#
# 数据（config.toml / pikpak.db / session / 日志 / 缩略图）全部落在 /data，
# 挂一个卷就够，重建容器不丢东西。
#
#   docker build -t pikpak-collector .
#   docker run -d -p 8000:8000 -v $PWD/data:/data pikpak-collector

# ==========================================================================
# 前端构建
# ==========================================================================
# --platform=$BUILDPLATFORM 是关键：多架构构建时这一段跑在**构建机**上，
# 而不是被 QEMU 模拟的目标架构上。Vite 打包在模拟的 arm64 下要十几分钟，
# 产物又和架构无关，没必要陪跑。
FROM --platform=$BUILDPLATFORM node:22-alpine AS web
WORKDIR /web
# 先只拷清单：依赖没变时这一层能命中缓存，改代码不用重装 node_modules
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ==========================================================================
# 运行时
# ==========================================================================
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIKPAK_CONFIG=/data/config.toml

WORKDIR /app

# 系统时区库：slim 镜像里没有 /usr/share/zoneinfo，缺了它 TZ 环境变量不生效，
# 容器的「本机时区」会一直是 UTC。requirements 里的 tzdata 是 Python 包，
# 只够 zoneinfo 用，管不到 libc。
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY tools/ ./tools/
COPY config.example.toml ./
COPY docker/config.toml ./docker/config.toml
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --from=web /web/dist ./web/dist

# 日志和缩略图在代码里是相对 ROOT 的路径，指到数据卷上。
# 放在构建期做（而不是 entrypoint 里 rm + ln）是为了能安全地 bind mount：
# 运行时删目录会把挂载点一起删掉。
RUN mkdir -p /data/logs /data/media /data/session \
 && ln -s /data/logs /app/logs \
 && ln -s /data/media /app/media \
 && chmod +x /usr/local/bin/entrypoint.sh

VOLUME ["/data"]
EXPOSE 8000

# 容器里必须监听 0.0.0.0，否则端口映射不出去。
# docker/config.toml 里已经写好，这里再兜一层，防用户改配置时漏掉
# （本地 config.toml 直接拷进容器是常见操作）。
ENV PIKPAK_HOST=0.0.0.0

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u,sys; sys.exit(u.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status!=200)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
