# 使用精简轻量 Python 运行时
FROM python:3.11-alpine

# 设置非 root 用户提升容器安全性
RUN addgroup -S appgroup && adduser -S appuser -G appgroup

WORKDIR /app

# 复制项目代码
COPY . /app

# 确保文件权限安全
RUN chown -R appuser:appgroup /app

USER appuser

# 暴露运行端口
EXPOSE 8686

# 启动加固后的 server.py
CMD ["python", "server.py"]
