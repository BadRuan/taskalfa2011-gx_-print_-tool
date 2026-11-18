# ============================================================
# TASKalfa 2011 局域网打印服务 · 镜像定义
#
# 构建：./run.sh（内部执行 podman build -t taskalfa-print:latest .）
# 运行：./run.sh（run.sh 负责挂载持久化数据目录，见下方 DATA）
#
# 目录约定（重要）：
#   项目设计文件（代码/文档/实验）→ ~/Documents/taskalfa-print（本仓库，纳入 git）
#   持久化数据（作业/预览/jobs.json）→ ~/Documents/Docker/tasklafa_print/data
#   数据目录不进镜像、不进 git，由 run.sh 以 -v .../data:/data:Z 挂载；
#   挂载源路径在创建容器时写死，移动数据后必须 ./run.sh 重建容器。
#
# 已验证环境：Fedora 45 + rootless podman 6.1.3 + quay.io（docker.io 不可达）
# ============================================================
FROM quay.io/fedora/fedora:latest

# PYTHONUNBUFFERED：python 日志实时可见；DATA_DIR 必须与 run.sh 的挂载点一致
ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

# 打印链路依赖：
#   python3 + Pillow      服务与图像/PDF 处理
#   ghostscript           cups 设备生成 CUPS raster（必须 8bpc 灰度，见技术档案 6 节）
#   poppler-utils/psutils PDF 工具
#   libreoffice-writer    Office 文档转 PDF
#   cups-libs             滤镜运行时依赖
#   fontconfig + Noto CJK 中文字体（否则中文变方框）
RUN dnf -y install --setopt=install_weak_deps=False \
        python3 \
        python3-pillow \
        ghostscript \
        poppler-utils \
        psutils \
        libreoffice-writer \
        cups-libs \
        fontconfig \
        google-noto-sans-cjk-fonts \
    && dnf clean all \
    && rm -rf /var/cache/dnf

WORKDIR /app

# 服务代码 + 京瓷官方滤镜（--chmod 直接在 COPY 层设执行位，省一层）
COPY --chmod=755 app/ /app/
COPY --chmod=755 rastertokpsl_A3 /app/rastertokpsl_A3

# 兜底目录；真实数据来自挂载卷，镜像内不存放任何业务数据
RUN mkdir -p /data/jobs

EXPOSE 8080

# 健康检查跟随 WEB_PORT（默认 8080），与服务端读取逻辑一致。
# 注意：podman build 默认 OCI 格式会忽略 HEALTHCHECK，run.sh 已加 --format docker。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('WEB_PORT', '8080'), timeout=4)"

# 容器内以 root 运行：rootless podman 会把它映射回宿主 uid 1000，
# 才能写入宿主持久化目录 /data（改非 root 会 PermissionError，勿动）
ENTRYPOINT ["python3", "-u", "/app/server.py"]
