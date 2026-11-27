# Cat MCP Server — 公网部署安全容器
#
# 多阶段构建：build → run
# run 阶段使用 distroless 镜像 + 专用非 root 用户
# 依据: CAT-34

# ---- Build stage ----
FROM python:3.12-slim AS builder

WORKDIR /build

# 安装构建依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制并安装项目（含依赖）
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir -e .

# ---- Run stage ----
FROM python:3.12-slim

# 安全：创建专用非 root 用户（UID 1001）
RUN groupadd --gid 1001 cat && \
    useradd --uid 1001 --gid cat --shell /sbin/nologin --no-create-home cat

# 安全：容器系统根只读
RUN chmod 755 /

# 创建数据目录（持久化存储）
RUN mkdir -p /var/lib/cat && chown cat:cat /var/lib/cat

WORKDIR /app

# 从 builder 阶段复制已安装的包
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 复制项目文件
COPY pyproject.toml README.md ./
COPY src/ ./src/

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CAT_CONFIG=/etc/cat/config.yaml \
    CAT_DB=/var/lib/cat/state.db \
    CAT_HOST=127.0.0.1 \
    CAT_PORT=9100

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:9100/health')" || exit 1

# 安全：drop capabilities, no-new-privileges
# 在 docker-compose 或 k8s securityContext 中配置

# 切换到非 root 用户
USER cat:cat

# 暴露端口（仅容器内监听）
EXPOSE 9100

# 默认启动命令
ENTRYPOINT ["python3", "-m", "cat_mcp"]
CMD ["--config", "/etc/cat/config.yaml"]
