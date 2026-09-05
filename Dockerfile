FROM python:3.11-slim

# 相似图去重需要 ffmpeg 生成感知哈希缩略图
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY tmdb_provider.py ./

# 配置不进镜像：api_key 等全部通过环境变量在运行时注入（见 README 配置表）
ENV TZ=Asia/Shanghai \
    TMDB_BIND=0.0.0.0
VOLUME ["/app/cache", "/app/logs"]
EXPOSE 38080

CMD ["python3", "tmdb_provider.py"]
