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

## 部署（目标机：fedora）

```bash
# 1. 拿到代码（git 克隆，或把目录同步过去）
git clone git@gitee.com:ruanfumin/taskalfa2011-gx_-print_-tool.git ~/Documents/taskalfa-print

# 2. 构建 + 重启 + 健康检查（自动创建数据目录、构建镜像、重建容器、curl 健康检查）
cd ~/Documents/taskalfa-print && ./run.sh

# 3. 打开界面
#    http://<fedora-ip>:8080/       （容器用 --network=host）
```

改代码后只需重复第 2 步（代码是 COPY 进镜像的，必须重建镜像）。
**作业记录不会丢**：数据在仓库外的 `~/Documents/Docker/tasklafa_print/data/`，容器删了重建也不影响。
旧机器迁移时，把整个 `data` 目录拷回原路径再 `./run.sh` 即可。

`run.sh` 还支持：

| 命令 | 作用 |
|---|---|
| `bash run.sh` | 构建镜像 + 重启容器 + 健康检查 |
| `bash run.sh logs` | 跟踪容器日志 |
| `bash run.sh stop` | 停止并删除容器 |
| `bash run.sh state` | 查看容器状态 |

环境变量（`run.sh` 里已设好默认值）：`PRINTER_IP=192.168.0.199`、`PRINTER_PORT=9100`、`WEB_PORT=8080`、`DATA_DIR=/data`。

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
