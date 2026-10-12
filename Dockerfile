# 毕业论文格式流水线 · 服务器镜像（x86_64 / ARM64 通用）
#
# 基础镜像是多架构的，在哪种机器上 build 就得到哪种镜像。
# 部署到 Oracle Cloud ARM 实例时，请在实例本机 build，不要在本机 build 完再拷过去。
#
# 构建（首次约 15~30 分钟，镜像约 3~4 GB）：
#     docker compose -f docker-compose.server.yml build
# 运行：
#     docker compose -f docker-compose.server.yml up -d
#
# 中文字体（重要）：
#   Windows 的 宋体/黑体/楷体/仿宋 有版权，不能打进镜像。
#   这里装开源替代：Noto Serif CJK SC（≈宋体）、Noto Sans CJK SC（≈黑体）、
#   文鼎楷体 AR PL UKai CN（≈楷体）、文鼎明体 AR PL UMing CN（≈仿宋）、
#   Liberation Serif（≈Times New Roman）。
#   format.sty 里用 \IfFontExistsTF 依次探测，本机有 Windows 字体就用 Windows 字体，
#   服务器上自动回退到这批开源字体，两边都能编译。
FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

# TeX Live（XeLaTeX + 中文）与 LibreOffice（取目录真页码 / 生成封面页）
# texlive-latex-extra 提供 tocloft / booktabs / enumitem / pdfpages 等宏包
RUN apt-get update && apt-get install -y --no-install-recommends \
        texlive-xetex \
        texlive-latex-recommended \
        texlive-latex-extra \
        texlive-lang-chinese \
        texlive-fonts-recommended \
        texlive-plain-generic \
        texlive-pictures \
        libreoffice-writer \
        fontconfig \
        fonts-noto-cjk \
        fonts-arphic-ukai \
        fonts-arphic-uming \
        fonts-liberation \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 可选：宿主机有 Windows 字体时挂载到这里，会被优先使用
RUN mkdir -p /usr/share/fonts/windows

EXPOSE 8000

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
