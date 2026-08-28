# MCP server for deepseek-as-subagent
# Used by Glama (https://glama.ai/mcp/servers) for automated introspection checks.
# Local users should still use install.sh — this image runs the bare MCP server
# without skill / slash-command / Claude registration.

FROM python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17

WORKDIR /app

# 缓存层：先 copy 元数据，下次代码改动时跳过装依赖
COPY pyproject.toml ./
COPY requirements.lock ./
COPY README.md ./
COPY LICENSE ./
COPY src ./src

# Verify every third-party distribution against the reviewed cross-platform
# lock, then install this source tree without dependency resolution or an
# isolated build environment.
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock \
    && python -m pip install --no-cache-dir --no-deps --no-build-isolation . \
    && python -m pip check \
    && install -d -o 65532 -g 65532 -m 0700 /home/deepseek

# Keep the package/runtime root-owned and run introspection without root.
ENV HOME=/home/deepseek \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 65532:65532

# MCP server 走 stdio，无端口暴露
# 没 DEEPSEEK_API_KEY 时 ping 返回 NOT_CONFIGURED 而非 crash —— 适合 Glama
# introspection（仅启动 + 列工具，不实际派工）
ENTRYPOINT ["deepseek-mcp"]
