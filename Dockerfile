FROM python:3.11-slim

WORKDIR /app
# --chmod：宿主机源码可能是 600（仅属主可读），容器内以 1000:1001 运行必须放开读权限
COPY --chmod=0644 tmdb_provider.py ./

# 配置不进镜像：api_key 等全部通过环境变量在运行时注入（见 README 配置表）
# 不装 ffmpeg（省约 450MB）：/meta/images 候选图相似去重自动禁用，其余功能不受影响
ENV TZ=Asia/Shanghai \
    TMDB_BIND=0.0.0.0
VOLUME ["/app/cache", "/app/logs"]
EXPOSE 38080

CMD ["python3", "tmdb_provider.py"]
