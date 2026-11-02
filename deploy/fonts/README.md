# 服务器字体

封面“海口经济学院”使用 **方正舒体**，对应 `FZSTK.TTF`（字体族 `FZShuTi`）。必须与正文所需的宋体、黑体、楷体、Times New Roman 一起部署。

Dockerfile 与 deploy/Dockerfile.local 均将本目录字体复制进镜像并刷新字体缓存；因此镜像独立运行时也包含字体。Compose 已取消字体目录挂载，避免服务器的空目录遮住镜像字体。字体更新需重新构建镜像；迁移已构建镜像时不必另行挂载字体。

字体二进制被 Git 忽略，单纯 git pull 不会取得字体。独立迁移包在 out/server-fonts.zip，内含文件 SHA256 清单。在服务器仓库根目录解压即可得到 deploy/fonts/ 下的字体，然后重新构建镜像。字体来源为本机 Windows Fonts，本包供本项目部署使用。

本地更新：`docker compose -f docker-compose.local.yml up -d --build app`；生产更新：`docker compose up -d --build app`。

核对：`docker compose exec app fc-match FZShuTi` 应返回 FZSTK.TTF；`fc-match 方正舒体` 同样应匹配 FZShuTi。还需分别核对 SimSun、SimHei、KaiTi、Times New Roman。旧排版结果不会自动更新。
