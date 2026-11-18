# TASKalfa 2011 局域网打印服务

在 Linux（fedora + rootless podman）上把 PDF / Office / 图片直接打印到 **京瓷 TASKalfa 2011**，
不依赖 CUPS 队列、不依赖 Windows 驱动，直接生成打印机原生的 **KPSL 光栅流** 走 9100 端口发过去。

自带中文 Web 界面：拖文件 → **上传即出打印预览** → 确认无误再下单打印。

> 完整的技术调研、协议逆向过程、踩坑记录都在 [技术档案.md](技术档案.md)，本文件只讲「怎么用、怎么部署、目录里都是啥」。

---

## 它解决的核心问题

京瓷这台机器只认自家驱动，Linux 下没有可用驱动（官方 Linux 驱动包只提供 32/64 位滤镜 + PPD，且依赖 CUPS）。
本项目把整条链路自己实现了一遍：

```
PDF/Office/图片
   │  gs -sDEVICE=cups               ← 600dpi、可打印区尺寸、FIXEDMEDIA、8bpc 灰度
   ▼
CUPS raster（页头 1796B + 位图）
   │  rastertokpsl_A3（京瓷官方滤镜）
   ▼
KPSL 流（LSPK + J/D/C/S/G/P/B…/F/E/T 若干 chunk）
   │  补 3 个字节级修正（S.dw2 / 双面 / 页头 Duplex）
   ▼
TCP 192.168.0.199:9100
```

三个**必须**满足的条件（当初折腾最久的部分）：

1. **raster 必须 8bpc 灰度**（`-dcupsBitsPerColor=8 -dcupsColorSpace=3`）。
   1bpp 时打印机把每行字节数当成 600dpi 的点数来解释，横向被压成 1/8 幅——这就是最初「打印内容尺寸变形」的根因。
2. **页面尺寸用可打印区**（A4 = 571×822pt，取自 PPD 的 `ImageableArea`），并加 `-dFIXEDMEDIA`。
3. **滤镜参数只传 `PageSize=`**，不要传 `Resolution=`；调完滤镜再把 S 块第 3 个双字补成 `0x10000`
   （Windows 驱动恒定如此，官方滤镜默认写 0）。

## 目录结构

```
Dockerfile                    # 镜像：fedora + python3/Pillow/ghostscript/libreoffice + 官方滤镜
                              #   （原名 Containerfile，2026-11 更名，docker/podman 通用）
.dockerignore                 # 构建上下文只留 app/ 与 rastertokpsl_A3
.gitignore                    # 兜底：data/、日志、__pycache__ 不入库
run.sh                        # 构建、重启容器、健康检查（./run.sh，脚本有执行位）
app/server.py                 # 服务主程序：HTTP API + 转换 + KPSL 生成 + 作业队列
app/ui.html                   # 中文单页 UI（含打印预览）
rastertokpsl_A3               # 京瓷官方滤镜（ELF x86-64，217KB）
kyocera_linuxdrv_..._v8.1305.zip  # 官方驱动包（PPD + 滤镜 + 安装脚本，逆向依据）
技术档案.md                    # ★ 全部技术结论、公式、踩坑清单
captures/                     # Windows 驱动正常打印的 9100 抓包（KPSL 金标准样本）
experiments/                  # 调试期的对照流（C/D/E.kpsl、标定页 cal.pdf、t1~t7.kpsl）
tools/                        # 解析 / 打补丁 / 发送 的一次性脚本
capture_proxy.py              # 抓包代理：8080 → 打印机 9100，落盘原始字节
```

**持久化数据不在仓库里**（2026-11 起的目录约定）：

```
~/Documents/taskalfa-print/                    # 本仓库：设计文件（代码/文档/实验），入 git
~/Documents/Docker/tasklafa_print/data/        # 容器 /data 卷：jobs/、preview/、jobs.json、testkit/
                                               #   不入 git，run.sh 以 -v .../data:/data:Z 挂载
```

> ⚠️ 挂载源路径在**创建容器时写死**。以后再移动数据目录，必须重新 `./run.sh` 重建容器，
> 否则 `podman start` 报 `getxattr ...: no such file or directory`（2026-11-18 踩过）。

## 部署指南（Podman / Docker）

### 端口与目录约定（部署前先看这个）

| 项 | 值 | 说明 |
|---|---|---|
| **Web 服务端口** | **8080/tcp** | 唯一需要对外开放的端口。容器内 `EXPOSE 8080`、监听 `WEB_PORT`（默认 8080）；本项目默认 `--network=host`，所以宿主直接占用 8080 |
| 打印机数据口（出向） | `192.168.0.199:9100/tcp` | 服务主动连打印机发 KPSL 流，**不需要映射、不需要开防火墙** |
| 打印机 SNMP（出向） | `192.168.0.199:161/udp` | 查在线状态 / 面板文本 / 累计页数 |
| **目录映射** | `<宿主>…/data` → 容器 **`/data`** | 作业、预览、`jobs.json`、测试素材全在这里；**容器删了重建数据也不丢**，镜像里没有任何业务数据 |
| 其它入站端口 | 无 | 容器不需要其它任何入站端口 |

- 默认访问地址：`http://<主机IP>:8080/`
- 防火墙只需放行 8080（firewalld）：
  ```bash
  sudo firewall-cmd --add-port=8080/tcp --permanent && sudo firewall-cmd --reload
  ```

### 方式一：一键脚本（Podman，推荐）

```bash
# 1. 拿到代码
git clone git@gitee.com:ruanfumin/taskalfa2011-gx_-print_-tool.git ~/Documents/taskalfa-print
cd ~/Documents/taskalfa-print

# 2. 构建 + 重启 + 健康检查
./run.sh
```

`./run.sh` 依次做：`mkdir -p` 数据目录 → `podman build --format docker -f Dockerfile` → 删旧容器 → `podman run`（参数见下）→ `curl /api/health` 确认。

> 以后改代码只需重跑 `./run.sh`。**作业记录不会丢**：数据在仓库外的
> `~/Documents/Docker/tasklafa_print/data/`，容器删了重建也不影响。

### 方式二：手动 podman 命令（与 run.sh 核心等价）

```bash
# 数据目录先建好（挂载源必须存在）
mkdir -p /home/<你>/Documents/Docker/tasklafa_print/data

# 构建镜像（--format docker 是为了启用 Dockerfile 里的 HEALTHCHECK）
podman build --format docker -t taskalfa-print:latest .

# 启动容器
podman run -d --name taskalfa-print \
  --restart=unless-stopped \
  --network=host \
  -e PRINTER_IP=192.168.0.199 \
  -e PRINTER_PORT=9100 \
  -e WEB_PORT=8080 \
  -e TZ=Asia/Shanghai \
  -v /home/<你>/Documents/Docker/tasklafa_print/data:/data:Z \
  taskalfa-print:latest
```

参数含义：

- `--network=host`：容器直接用宿主网络栈，8080 监听在宿主上（**此时 `-p` 端口映射无效**）。本服务要用 SNMP 查状态、主动连打印机 9100，host 网络最省事，也是本项目默认写法。
- `-v …/data:/data:Z`：**唯一必须的目录映射**。`:Z` 是 SELinux 重打标签（Fedora/RHEL 必须，否则 `PermissionError: '/data/jobs'`）；Debian/Ubuntu 无 SELinux 可去掉 `:Z`。
- `--restart=unless-stopped`：宿主重启后自动拉起容器。
- 容器名 `taskalfa-print`、镜像名 `taskalfa-print:latest`：`run.sh` 与本文档都按这个名字引用。

### 方式三：Docker（不用 host 网络，用端口映射）

```bash
docker build -t taskalfa-print:latest .

docker run -d --name taskalfa-print \
  --restart=unless-stopped \
  -p 8080:8080 \
  -e PRINTER_IP=192.168.0.199 \
  -e PRINTER_PORT=9100 \
  -e WEB_PORT=8080 \
  -e TZ=Asia/Shanghai \
  -v /path/to/data:/data \
  taskalfa-print:latest
```

- `-p 8080:8080` = `宿主端口:容器端口`。容器内监听的是 `WEB_PORT`，**改端口时 `WEB_PORT` 和 `-p` 要一起改**（例如 `-e WEB_PORT=9090 -p 9090:9090`）。
- Docker 镜像里的 `HEALTHCHECK` 原生生效；`--network=host` + `-p` 二选一，不要同时用。

### 方式四：compose（docker compose / podman-compose 通用）

```yaml
# docker-compose.yml（放在仓库根目录）
services:
  taskalfa-print:
    build: .
    image: taskalfa-print:latest
    container_name: taskalfa-print
    restart: unless-stopped
    network_mode: host          # 想改成端口映射：删这行，换成 ports: ["8080:8080"]
    environment:
      PRINTER_IP: 192.168.0.199
      PRINTER_PORT: "9100"
      WEB_PORT: "8080"
      TZ: Asia/Shanghai
    volumes:
      - /home/ruanfumin/Documents/Docker/tasklafa_print/data:/data:Z   # 无 SELinux 的系统去掉 :Z
```

```bash
docker compose up -d --build      # 或 podman-compose up -d --build
```

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PRINTER_IP` | `192.168.0.199` | 打印机 IP |
| `PRINTER_PORT` | `9100` | 打印机数据端口 |
| `WEB_PORT` | `8080` | 服务在**容器内**监听的端口 |
| `TZ` | `Asia/Shanghai` | 时区（任务列表的打印时间按它显示） |
| `DATA_DIR` | `/data` | 容器内数据目录，必须与挂载点一致，**不要改** |

### 日常运维

| 操作 | Podman（run.sh） | 等价手动命令 |
|---|---|---|
| 构建 + 重建 + 健康检查 | `./run.sh` | `podman build … && podman rm -f … && podman run …` |
| 看日志 | `./run.sh logs` | `podman logs -f --tail 100 taskalfa-print`（docker 同名替换） |
| 停止并删除容器 | `./run.sh stop` | `podman rm -f taskalfa-print`（**数据不受影响**） |
| 看容器状态 | `./run.sh state` | `podman ps -a --filter name=taskalfa-print` |
| 健康检查 | — | `curl http://127.0.0.1:8080/api/health` → `{"ok": true}` |
| 健康状态 | — | `podman inspect taskalfa-print --format '{{.State.Health.Status}}'` → `healthy` |

### 数据备份与迁移

```bash
# 备份（整个数据目录，含作业记录与历史预览）
tar czf taskalfa-data-$(date +%F).tgz -C /home/<你>/Documents/Docker/tasklafa_print data

# 恢复 / 换机器：解包回原路径，再 ./run.sh 重建容器
tar xzf taskalfa-data-2026-11-18.tgz -C /home/<你>/Documents/Docker/tasklafa_print/
cd ~/Documents/taskalfa-print && ./run.sh
```

> ⚠️ **挂载源路径在创建容器时写死**。以后再移动数据目录，必须重新 `./run.sh` 重建容器，
> 否则 `podman start` 报 `getxattr /旧路径/data: no such file or directory`（2026-11-18 踩过）。

### 故障排查

| 现象 | 原因与解法 |
|---|---|
| `podman start` 报 `getxattr …/data: no such file or directory` | 挂载源目录被移动/删除 → 把数据放回原路径，或改 `run.sh` 里的 `DATA`，然后 `./run.sh` 重建容器 |
| `PermissionError: '/data/jobs'` | SELinux 拦截 → 挂载少了 `:Z`，重建容器 |
| 页面打不开 8080 | 容器没起来（`podman ps`）/ 防火墙没放行 8080 / `WEB_PORT` 与映射端口不一致 |
| 状态卡显示打印机离线 | `PRINTER_IP` 是否正确、9100/161 出向是否通、打印机是否休眠或无纸 |
| 构建时拉不到基础镜像 | 本环境 `docker.io` 不可达，基础镜像走 `quay.io/fedora/fedora`（Dockerfile 已写） |

## Web 界面

- 打印机状态卡：在线/离线、型号、SNMP 设备状态、面板文本、累计页数（4 秒轮询）。
- 上传：拖拽或点击，多文件会**按顺序合并成一个作业**。
- 参数：单面 / 双面长边 / 双面短边、份数、纸张（A4/A3/A5/Letter/Legal）、页码范围、是否「适应纸张大小」。
  分辨率固定 600dpi（打印机原生），不再暴露 300dpi 选项。
- **打印预览**：选完文件（或改纸张/适应纸张）自动渲染。预览和正式打印共用同一套几何参数
  （可打印区尺寸 + `-dFIXEDMEDIA` + `-dPDFFitPage` 或 `PageOffset` 裁边），
  所以白边、裁切、缩放与真实出纸一致。右侧缩略图切页。
- 打印：点「开始打印」时提交预览 token，服务端直接复用已转换好的 PDF，不再重传文件。
- 最近任务表：文件名、大小、份数、单双面、纸张、状态、说明、打印时间、来源 IP。

### HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Web 界面 |
| GET | `/api/health` | `{"ok": true}` |
| GET | `/api/state` | 打印机状态 + 最近作业列表 |
| POST | `/api/preview` | multipart：`file`(可多个) / `paper` / `fit` → 预览 token + 每页 PNG 地址 |
| GET | `/api/preview/<token>/p<N>.png` | 预览图（灰度 PNG，120dpi） |
| POST | `/api/print` | multipart：`token` 或 `file` + `copies`/`duplex`/`paper`/`pages`/`fit` |
| POST | `/api/test` | 打印内置测试页 |

## 工具脚本（`tools/`）

| 脚本 | 用途 |
|---|---|
| `kpsl_dump.py` / `kpsl_compact.py` | 按 chunk 解析 KPSL 流，打印各字段（compact 版只打印头部若干双字） |
| `lspk_parse.py` | 早期版本 LSPK 解析器 |
| `patch_chunk.py <file> <tag> <idx> <value>` | 通用补丁：把某个 chunk 的第 idx 个双字改成 value |
| `patch_s.py <file> <value>` | 专改 S 块第 3 个双字（正常应恒为 `0x10000`） |
| `patch_b_dw12.py <file> <value>` | 批量改所有 B 块的第 12 个双字 |
| `patch_raster_w.py <raster> <width>` | 改 CUPS raster 页头的 `cupsWidth` |
| `send_kpsl.py <file>` | 把 KPSL 文件直接发到打印机 9100（在 fedora 上执行） |
| `make_cal_ps.py` / `make_scale_ps.py` | 生成毫米标定页 PostScript，用于验证尺寸/缩放/边距 |

## 已知限制

- 只针对 TASKalfa 2011（驱动包是 TASKalfa 1800/2200 系列的 GDI 滤镜，同源）。
- 预览最多渲染 12 页，预览产物保留 24 小时后自动清理。
- 打印机忙时会拒绝连接，服务端重试 3 次、间隔 5 秒；仍失败则作业报错。
- 打印机**无纸/卡纸**时作业会被静默丢弃（页面计数不涨），服务端无法感知，只能看面板。
- A3 / A5 / Letter / Legal 的可打印区按 PPD 表计算，几何已离线核对，但未全部真机出纸验证。
