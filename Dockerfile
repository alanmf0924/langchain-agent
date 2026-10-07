# 使用锁文件安装运行时依赖，确保容器与本地联调使用同一组 Python 包版本。
FROM ghcr.io/astral-sh/uv:0.9-python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# 先复制依赖描述文件，使业务代码变更不必重复下载依赖。
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# 将当前项目安装进已锁定的运行时环境；开发/测试依赖不会进入镜像。
RUN uv sync --locked --no-dev

EXPOSE 8000

# 迁移与管理员 bootstrap 均通过一次性 Compose 命令执行，应用启动不改动 Schema。
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
