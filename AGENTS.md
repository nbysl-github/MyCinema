# MyCinema - AI Agent 开发指南

## 项目概述

MyCinema 是一个基于 **FastAPI + Jinja2** 的本地视频流媒体服务器，提供 B站风格（Bilibili Design）的 Web 界面，支持视频浏览、播放、转码和分类管理。

**技术栈**: Python 3.12 / FastAPI / Protocol Buffers / OpenCV / FFmpeg / JavaScript (Vanilla) / CSS

---

## 项目结构

```
d:\OpenCode\MyCinema\
├── server.py              # 核心后端（所有 API 路由、扫描逻辑、转码、缓存）
├── pb_utils.py            # Protocol Buffers 读写工具（JSON→PB 迁移、通用读写接口）
├── config.pb              # 用户配置（Protocol Buffers 二进制格式）
├── categories.pb          # 分类配置（合集/MV/哔哩哔哩/KTV）
├── series_cache.pb        # 目录扫描缓存（含每个视频的缩略图URL）
├── video_meta_cache.pb    # 视频元数据缓存（分辨率、时长等）
├── thumbnail_cache.pb     # 缩略图生成状态缓存
├── hidden_series.pb       # 隐藏视频列表
├── *_pb2.py               # Protocol Buffers 生成的 Python 模块
├── protos/                # .proto 定义文件
├── backup_latest/         # 导出备份目录（自动生成）
│   ├── config.pb
│   ├── categories.pb
│   ├── thumbnail_cache.pb
│   ├── video_meta_cache.pb
│   ├── hidden_series.pb
│   ├── series_cache.pb
│   └── export_info.pb
├── ffmpeg/                # 内置 FFmpeg 工具集
│   ├── ffmpeg.exe
│   ├── ffplay.exe
│   └── ffprobe.exe
├── static/
│   ├── icons.js           # 本地 Lucide 图标（所有 SVG 内联）
│   └── css/bilibili.css   # B站风格样式
├── templates/
│   ├── index.html         # 首页（视频卡片网格、搜索、分类筛选、收藏）
│   ├── video.html         # 播放页（自定义控件、进度条、选集列表、外挂字幕）
│   ├── detail.html        # 详情页（视频元信息、集数列表）
│   ├── settings.html      # 设置页（分类管理、高级设置、后台任务、数据管理、使用说明）
│   └── 404.html           # 404 页面
└── run app.bat            # Windows 启动脚本
```

---

## 核心架构

### 1. 目录扫描与分类规则

**文件**: [server.py](server.py) — `scan_dir_for_series()`、`scan_dir_recursive()`、`_build_video_entry()`

#### 1.0 四种目录类型

`scan_dir_for_series()` (`server.py`) 根据目录内容判断类型：

| 类型 | 条件 | 扫描结果 | 首页展示 |
|------|------|----------|----------|
| **跳过(Skip)** | 无直连视频 + 无含视频子目录 | `return []` | 不显示 |
| **合集容器(Collection)** | 无直连视频 + 有含视频子目录 | `return []`，由递归处理子目录 | 子目录各生成独立卡片 |
| **混合(Mixed)** | 有直连视频 + 有含视频子目录 | 每个直连视频生成独立 `is_flat` 条目 | 多个独立卡片 |
| **扁平(Flat)** | 有直连视频 + 无含视频子目录 | **一级目录**：每个视频独立卡片；**合集/混合下的子目录**：整个目录作为一个合集卡片 | 见下 |

**判断流程**:
```
scan_dir_for_series(dir_path)
│
├─ 遍历目录：
│   ├─ videos[]              ← 直连视频文件
│   └─ subdirs_have_videos   ← 子目录中是否有视频（仅检测一层）
│
├─ 无直连视频 AND 无含视频子目录？ → [跳过] return []
├─ has_videos=True AND has_video_subdirs=True？ → [混合] 每个 video 独立成 is_flat 卡片
├─ has_videos=False AND has_video_subdirs=True？ → [合集容器] 返回 []
└─ has_videos=True AND has_video_subdirs=False？ → [扁平] 每个 video 独立成 is_flat 卡片
```

#### 1.0.1 扁平目录的两种行为（关键区分点）

```
扁平目录
├── 一级目录（_parent_is_mixed=False, _in_collection=False）
│   └── 每个视频独立卡片（episode_count=1）
│
└── 合集/混合下的子目录（_parent_is_mixed=True 或 _in_collection=True）
    └── 整个目录作为一个合集卡片（episode_count=N）
```

**行为差异**：一级扁平目录将每个视频拆分为独立 series（`episode_count=1`），而合集容器或混合目录下的扁平子目录则将所有视频打包为一个合集 series（`episode_count=N`）。

#### 1.0.2 递归扫描上下文传递

`scan_dir_recursive()` (`server.py`) 的 `_walk` 内部函数在递归子目录时传递两个上下文标志：

- **`_in_coll`**：`_in_coll or is_mixed or is_collection` — 只要祖先中出现过混合或合集容器，后续所有子目录都会被视为"合集内"
- **`_parent_is_mixed`**：`is_mixed` — **仅向下一层传递**，标识"直接父目录是混合目录"

#### 1.0.3 分类分配逻辑

`_get_all_series_uncached()` (`server.py`)：

1. **分类配置目录**（`categories.pb` 中 `dirs` 列表）→ 分配到对应分类
2. **VIDEO_BASE_DIR 下的其他目录**（未被任何分类覆盖）→ 分配到默认分类 `{id:"default", name:"合集"}`

`get_series_category()` (`server.py`) 是 fallback，检查目录名是否在分类 `dirs` 列表中。

**未分类区域**：`unassigned_series`（`server.py`）定义为属于 `default` 分类且 `dirs` 列表为空的 series，在首页显示为"未分类"分区。

#### 1.0.4 关键规则

- **不再依赖目录名中的 `(N集)` 标记**：分类完全基于目录结构判断
- 最大递归深度: `max_depth=3`
- **缩略图URL始终构造**：扫描时为每个视频构造 `{视频名}_thumb.jpg` 的 URL
- 提取了 `_build_video_entry()` 辅助函数统一构造视频条目（含转码检测）

#### 1.0.5 首页卡片显示

`is_flat` 是 `scan_dir_for_series` 内部参数，**不在模板中作为条件判断**。实际上影响卡片显示的是：

- **`orientation`**（portrait/landscape）：决定封面比例
- **`episode_count`**：决定"X集"角标
- **`cover`**：决定封面图片

由于一级扁平目录的每个视频都生成独立 series（`episode_count=1`），首页会显示为多个"1集"的独立卡片。首页按分类分区显示，每个分类显示为一个 `<div class="category-section">`。

---

### 1.1 异步扫描架构（非阻塞）

**文件**: [server.py](server.py) — `get_all_series()`、`_refresh_series_cache()`、`_background_init()`

**设计原则**: 扫描操作绝不阻塞 FastAPI 事件循环和 HTTP 请求。

**锁机制**:
- `_series_list_cache_lock`: 保护 `_series_list_cache` 的读写（内存缓存锁，轻量）
- `_series_scan_lock`: 防止多个线程并发执行全量磁盘扫描（重量级）

**`get_all_series()` 流程**:
```
get_all_series()
│
├─ [锁内] TTL 内命中内存缓存？ → 直接返回（最快）
├─ [锁内] 自动扫描关闭？
│   ├─ 有内存缓存 → 返回缓存
│   ├─ 有 .pb 缓存文件 → 恢复到内存并返回
│   └─ 完全无缓存 → 释放锁
│       └─ [锁外] 执行同步扫描（首次启动唯一阻塞点）
├─ [锁内] 自动扫描开启？
│   ├─ 有缓存 → 启动后台刷新线程，立即返回旧缓存
│   └─ 无缓存 → 释放锁
│       └─ [锁外] 用 _series_scan_lock 防并发，执行同步扫描
```

**后台扫描**:
- `_background_init()`: 启动时在后台线程中调用 `get_all_series()` 预热缓存
- `_refresh_series_cache()`: TTL 过期后在后台线程中刷新缓存，不阻塞请求
- `_file_watcher_loop()`: 定时检测目录变化（仅在自动扫描开启时活跃）

### 1.2 播放列表合并与显示逻辑

**文件**: [server.py](server.py) — `play_episode()`、`_collect_mixed_dir_videos()`、`_merge_category_videos()`、`_get_category_siblings()`
**前端**: [templates/video.html](templates/video.html) — 剧集列表渲染、`switchEpisode()` 函数

#### 1.2.1 合并决策优先级（从高到低）

`play_episode()` (`server.py`) 中的播放列表构建逻辑：

```
播放某个视频时：
│
├─ 1. 合集容器内的子目录（in_collection=True, 无混合祖先）
│   └─ 不合并，只显示当前子目录的视频
│
├─ 2. 混合目录下的视频（自身或祖先目录是混合目录）
│   └─ 调用 _collect_mixed_dir_videos(mixed_dir_path)
│      合并该混合目录下所有视频（直连 + 子目录中的）
│
├─ 3. 默认分类（cat_id == 'default'）
│   └─ 调用 _merge_category_videos(series) → 内部判断后返回 []
│
└─ 4. 其它分类
    └─ 调用 _merge_category_videos(series)
       合并同分类下所有 series 的视频
```

#### 1.2.2 混合目录检测（向上遍历祖先）

`play_episode()` 中向上遍历祖先目录（`server.py`）：

- 遇到**混合目录** → 记录为合并范围，停止
- 遇到**合集容器** → 记录但**继续向上**查找（混合目录下的合集也应合并）
- 到达根目录 → 停止

#### 1.2.3 两个合并函数的共性

`_collect_mixed_dir_videos()` 和 `_merge_category_videos()` (`server.py`)：

- 按 `filepath` **去重**（`seen_fps` set）
- 每个合并视频添加字段：`_dir_name`（来源 series 名称）、`_local_ep`、`_play_href`、`_video_url`
- 判断 `s_is_flat = (episode_count <= 1 or _flat_base_path 存在)` 来决定视频 URL 构造方式

#### 1.2.4 单集展开逻辑

当 `episode_count == 1` 且路径中包含 `/` 时（`server.py`），查找同级目录下所有视频，展开为完整播放列表，设置 `_flat_base_path`。

#### 1.2.5 前端播放列表显示（`video.html`）

**剧集列表渲染**：

```jinja2
{% for v in series.videos %}
  {# URL 优先级: _video_url > _flat_base_path > series.path #}

  {# 合并时显示目录分隔标签 #}
  {% if _category_merged and v._dir_name %}
    {% if _dir_name 变化 %}
      <div class="ep-dir-divider">{{ v._dir_name }}</div>
    {% endif %}
  {% endif %}

  <a class="ep-item" ...>
    {# 未合并 或 无_dir_name：显示 "第X集 · 文件名" #}
    {# 合并且有_dir_name：只显示文件名 #}
    <span class="ep-num">{{ ep_display }}</span>
    <div class="ep-name">...</div>
  </a>
{% endfor %}
```

**`_category_merged` 对显示的影响**:

| 元素 | 未合并 | 已合并 |
|------|--------|--------|
| **导航栏标题** | `series.name - 第X集` | `category.name - 第X集` |
| **信息栏标题** | `series.name - 第X集` | `category.name - 第X集` |
| **剧集名称前缀** | 始终显示"第X集 ·" | 只在无 `_dir_name` 时显示"第X集 ·" |
| **目录分隔标签** | 不显示 | `_dir_name` 变化时显示 `<div class="ep-dir-divider">` |
| **返回按钮链接** | `/` | `/?cat=category_id` |

#### 1.2.6 `switchEpisode()` 切集（`video.html`）

SPA 无刷新切集流程：
1. 更新高亮 + 自动滚动
2. 立即替换 `video.src` 并播放
3. `history.pushState` 更新 URL
4. 后台 `fetch('/api/play-info/...')` 异步加载视频详情、字幕、缩略图
5. 更新信息栏和标题

### 2. 视频服务与转码

**文件**: [server.py](server.py) — `serve_video()`

**转码格式判断**:
- **直接播放**: `.mp4`（且编码为浏览器兼容格式：H.264/H.265/VP9/AV1/MPEG-2）
- **FFmpeg 检测非标 MP4**: `_is_mp4_browser_playable()` 通过 ffprobe 检测编码
- **FFmpeg 转码为 MP4**: `.mkv`, `.avi`, `.mov`, `.flv`, `.wmv`, `.webm` 等 19 种格式

**GPU 硬件转码（NVENC）**:
- 配置项 `gpu_transcode`（**默认开启** `True`）
- 启动时自动检测 FFmpeg 是否支持 `h264_nvenc` 编码器
- 预转码参数：`h264_nvenc -preset p4 -cq 23 -b:v 0`
- 实时流转码参数：`h264_nvenc -preset p1 -rc cbr -maxrate 10M -bufsize 20M`
- 速度提升约 5~10 倍，CPU 占用极低

**转码时机**:
1. `auto_transcode` 开关开启时（默认开启），扫描到非 MP4 视频自动加入转码队列
2. `auto_transcode` 关闭时，仅标记为需转码但不入队
3. 添加目录时检测：保存分类配置后，后台异步检测新目录中的视频
4. 后台转码线程：`_transcode_worker()` 从队列获取任务执行转码
5. 转码成功后自动生成缩略图
6. 转码成功后默认删除原文件（`delete_original_after_transcode` 默认 `True`）

**自动转码开关影响的三个入口**:
- `_idle_thumbnail_checker()` — 空闲检测时的转码入队
- `_detect_and_queue_transcode()` — 增量扫描时的转码检测与入队
- `_transcode_worker()` — 后台转码线程的主动扫描入队

**转码参数** (`_transcode_video_file()`):
- **CPU 模式**（`libx264`）: `-preset medium -crf 23`
- **GPU 模式**（`h264_nvenc`）: `-preset p4 -cq 23 -b:v 0`

### 3. 前端播放器

**文件**: [templates/video.html](templates/video.html)

**核心特性**:
- 自定义控制栏（屏蔽原生 controls），包含：播放/暂停、上一集/下一集、可拖动进度条、分辨率标签、音量控制、倍速播放、外挂字幕切换、全屏
- **全部控制按钮使用 Lucide SVG 图标**（通过 `icons.js` 渲染引擎）
- 点击视频任意位置切换暂停/播放（排除控件区域）
- **自动播放下一集**：视频结束时直接 `switchEpisode(currentEp + 1)`
- **切集自动滚动**：`switchEpisode(index, autoScroll)` 参数控制是否自动滚动列表到当前集
- **方向自适应**：检测视频宽高比，横向视频切换为 1280x720 宽屏布局
- 防闪烁：页面加载时添加 `orient-pending` 类隐藏播放器
- 竖向音量滑块、倍速菜单（0.5x ~ 2.0x 七档可选）
- 键盘快捷键：空格（播放/暂停）、←/→（快退/快进10秒）、↑/↓（音量调节）、[ / ]（上一集/下一集）、F（全屏）
- 外挂字幕：自动扫描同名字幕文件（.srt/.ass/.vtt等）

**PiP 画中画浮窗**:
- 使用自定义 DOM 浮窗 (`#pipWindow`) 模拟画中画效果
- 支持拖动、缩放、控件交互
- 双模式切换：优先尝试浏览器原生 `requestPictureInPicture()` API，不可用时切换到自定义浮窗
- 控件显示/隐藏：鼠标悬停显示，2 秒后自动隐藏

### 4. 缓存机制

#### 4.1 Protocol Buffers 存储层

所有持久化数据使用 **Protocol Buffers** 二进制格式存储，取代旧版 JSON。

**存储文件** ([pb_utils.py](pb_utils.py) 提供统一读写接口):

| 缓存文件 | 用途 | Proto 定义 |
|----------|------|-----------|
| `config.pb` | 用户配置 | `protos/config.proto` |
| `categories.pb` | 分类配置 | `protos/categories.proto` |
| `series_cache.pb` | 目录扫描结果 | `protos/series_cache.proto` |
| `thumbnail_cache.pb` | 缩略图生成状态 | `protos/thumbnail_cache.proto` |
| `video_meta_cache.pb` | 视频元数据 | `protos/video_meta_cache.proto` |
| `hidden_series.pb` | 隐藏视频列表 | `protos/hidden_series.proto` |
| `export_info.pb` | 导出备份元信息 | `protos/export_info.proto` |

**迁移机制** (`pb_utils.run_migration()`):
- 启动时自动检测：如果 `.pb` 文件不存在但有对应 `.json` 文件，自动迁移
- 迁移后 `.json` 文件保留作为备份（可手动删除）
- 导入功能兼容旧版 `.json` 备份（优先读取 `.pb`，fallback 到 `.json`）

#### 4.2 多级缓存系统（L1 内存 → L2 磁盘 PB → L3 源文件）

**L1 内存缓存**（视频内容缓存）:
- `_video_content_cache`: 存储视频字节数据（LRU 淘汰，上限 50MB）
- TTL: 600 秒（10 分钟）
- 命中时直接返回内存中的数据，无需读取磁盘
- 支持 Range 请求缓存（每个 byte range 独立缓存）

**L2 磁盘缓存**（Protocol Buffers 二进制文件）:
| 缓存文件 | 用途 | 清除方式 |
|----------|------|----------|
| `series_cache.pb` | 目录扫描结果（含缩略图URL、封面URL、分辨率） | 删除后重启服务器重新扫描 |
| `thumbnail_cache.pb` | 缩略图生成状态缓存 | 删除后重新生成 |
| `video_meta_cache.pb` | 视频元数据持久缓存（分辨率、时长、文件大小、方向） | 删除后重新 ffprobe 采集 |
| `config.pb` | 用户配置 | 设置页编辑或手动编辑 |
| `hidden_series.pb` | 隐藏视频列表 | 手动编辑 |

**L3 源文件**（磁盘原始文件）:
- 当 L1/L2 缓存均未命中时，直接从磁盘读取源文件

#### 4.3 CDN 加速（HTTP 缓存头）

**Cache-Control 头**（仅视频文件）:
- `public, max-age=86400, immutable, stale-while-revalidate=3600`
- 浏览器缓存 24 小时，过期后允许使用旧内容（3600 秒内）
- 支持 ETag 验证（`If-None-Match` → 304 Not Modified）

**Range 请求优化**:
- 支持 HTTP Range 请求（断点续传）
- 每个 byte range 独立缓存（`{filepath}:{start}-{end}`）
- 206 Partial Content 响应

#### 4.4 视频预加载系统

**预加载策略**:
- 检测用户播放进度 > 80% 时，自动预加载下一集
- 后台工作线程异步处理（`_prefetch_worker()`）
- 预加载队列最大 10 个任务
- 使用 Fetch API 预加载元数据（HEAD 请求 + 前 1MB 数据）

**预加载配置**:
- `prefetch_enabled`: 预加载开关（默认 `true`）
- 预加载队列大小：`_prefetch_queue`

**内存缓存层次**:
- `_series_list_cache`: `get_all_series()` 的结果缓存，TTL 由 `cache_ttl` 配置控制（默认 300 秒）
- `_series_cache`: 目录级缓存（`{dir_path: {mtime, data}}`）
- `_categories_cache`: 分类配置缓存，按文件 mtime 失效
- `_video_meta_cache`: 视频元数据缓存（`{filepath: {width, height, duration, size, orientation, _mtime}}`）
- `_bitrate_cache`: 视频码率 LRU 缓存，上限 512 条
- `_video_content_cache`: L1 视频内容缓存（上限 50MB，TTL 600 秒）

**元数据补全系统** (`populate_all_video_meta()`):
- **统一入口**：所有调用方（后台自动/手动API/空闲检测）共用同一个函数
- **并行执行**：使用 `ThreadPoolExecutor` 并行采集（workers 由 `meta_workers` 配置控制，默认 4）
- **进度追踪**：`_meta_populate_progress` 字典记录 `{total, done, running, current, error}`
- **智能跳过**：跳过已有有效 `file_size` 的视频

**缩略图管理系统**:
- **生成**: `generate_thumbnail(video_path, verbose=True/False)` 使用 OpenCV 在视频 10% 处截取帧
- **验证修复**: `verify_and_regenerate_thumbnails()` 在空闲时检测缩略图尺寸
- **封面生成**: `generate_all_series_covers()` 为合集目录生成封面

**转码管理系统**:
- **队列**: `_transcode_queue` 列表，`_transcode_in_progress` 字典，`_transcode_progress` 进度字典（0~100）
- **后台线程**: `_transcode_worker()` 每 30 秒检查队列，逐个执行转码
- **进度追踪**: `-progress pipe:1` 解析 `out_time_us`，NVENC 时额外使用文件大小估算

### 5. API 端点

**主要端点**:
- `GET /` - 首页（视频卡片网格）
- `GET /play/{series_path:path}/{episode_index:int}` - 播放页面
- `POST /api/categories` - 保存分类配置（支持 `force_scan: true` 强制后台扫描）
- `GET /api/categories/export` - 导出所有缓存（.pb 格式）
- `POST /api/categories/import` - 导入所有缓存（兼容旧版 .json）
- `GET /api/auto-scan` - 获取自动扫描状态
- `POST /api/auto-scan` - 设置自动扫描状态
- `GET /api/play-info/{series_path}/{episode_index}` - 异步返回播放页详细信息
- `GET /api/meta/stats` - 获取元数据统计
- `POST /api/meta/populate` - 手动触发元数据补全
- `GET /api/transcode/status` - 获取转码状态
- `POST /api/transcode/cancel` - 取消转码
- `GET /api/hidden-series` - 获取隐藏列表
- `POST /api/hidden-series/restore` - 恢复隐藏系列
- `GET /api/server-info` - 获取服务器信息（GPU、转码队列、L1 缓存状态等）
- `GET /api/browse-dir?path=` - 浏览目录
- `GET /api/drives` - 获取可用磁盘
- `POST /api/config` - 保存配置
- `GET /api/config` - 获取配置
- `POST /api/clear-cache` - 清除扫描缓存
- `GET /video/{filepath}` - 视频流（支持 Range 请求）
- `GET /cover/abs/{dir_path}/{filename}` - 封面图片（绝对路径）
- `GET /cover/{series_path}/{filename}` - 封面图片（相对路径）
- `GET /subtitle/{series_path}/{episode_index}/{filename}` - 外挂字幕文件
- `GET /series/detail/{series_path}` - 系列详情

### 6. 配置系统

**文件**: [config.pb](config.pb)（Protocol Buffers 二进制格式）

**配置项**:
```json
{
  "auto_scan_enabled": true,
  "auto_transcode": true,
  "delete_original_after_transcode": true,
  "idle_check_interval": 300,
  "watcher_interval": 5,
  "cache_ttl": 300,
  "meta_workers": 4,
  "detail_workers": 8,
  "meta_cache_max": 50000,
  "gpu_transcode": true,
  "server_port": 5000,
  "server_host": "0.0.0.0",
  "prefetch_enabled": true,
  "cdn_cache_max_age": 86400,
  "l1_cache_max_size_mb": 50,
  "l1_cache_ttl": 600
}
```

**缓存配置项**:
- `prefetch_enabled`: 视频预加载开关（默认 `true`）
- `cdn_cache_max_age`: CDN 缓存最大时间（秒，默认 86400 = 24 小时）
- `l1_cache_max_size_mb`: L1 内存缓存上限（MB，默认 50）
- `l1_cache_ttl`: L1 缓存 TTL（秒，默认 600 = 10 分钟）

**可选配置项**:
```json
{
  "default_category": {
    "id": "default",
    "name": "合集",
    "icon": "tv",
    "color": "#fb7299"
  },
  "uncategorized_label": "未分类"
}
```

**分类配置** ([categories.pb](categories.pb)):
```json
{
  "categories": [
    {
      "id": "cat_1777123042130",
      "name": "短剧",
      "icon": "tv",
      "color": "#FB7299",
      "dirs": ["E:\\短剧"]
    }
  ]
}
```

**全局常量**:
- `ALLOWED_EXTENSIONS`: 支持的视频格式集合（21 种）
- `TRANSCODE_FORMATS`: 需要转码的视频格式集合（19 种）
- `SUBTITLE_EXTENSIONS`: 支持的字幕格式集合
- `MIME_MAP`: 视频文件 MIME 类型映射表
- `_bili_colors`: B站配色字典

**服务器配置变量**:
- `_server_port`: 服务器端口号（从配置读取，默认 5000）
- `_server_host`: 服务器绑定地址（从配置读取，默认 "0.0.0.0"）

**默认分类函数**:
- `_get_default_category()`: 获取默认分类配置，支持从配置自定义
- `_get_uncategorized_label()`: 获取未分类标签配置，支持从配置自定义

---

## 开发注意事项

### 扫描架构（重要）
- **`get_all_series()` 中的 `_get_all_series_uncached()` 必须在 `_series_list_cache_lock` 锁外执行**，否则会阻塞所有需要缓存的其他请求
- **`api_save_categories` 的增量扫描在后台线程执行**，不在事件循环中阻塞
- **`_file_watcher_loop` 每轮循环检查 `is_auto_scan_enabled()`**，关闭时跳过扫描
- **`POST /api/categories` 支持 `force_scan: true`**，重新扫描按钮使用此标志强制触发
- **`_series_scan_lock` 防止并发全量扫描**：多个线程同时发现无缓存时，只有一个执行扫描

### 缓存一致性
- 确保目录扫描后正确保存缓存（Protocol Buffers 格式）
- 处理扁平目录下单个视频的路径格式
- 缓存恢复时检查路径字段完整性
- 所有 `.pb` 文件通过 `pb_utils.py` 统一读写

### 错误处理
- 播放页面正确处理合集目录下的扁平子目录
- 增强路径标准化比较
- 前端 fetch 请求必须有 `.catch()` 错误处理，避免"加载中..."永远显示
- 目录浏览器使用 `reqId` 机制防止竞态条件（弹窗快速开关时旧请求覆盖新数据）

### 性能优化
- 后台异步扫描避免阻塞首页加载
- 缩略图和封面生成使用后台线程
- 缓存 TTL 机制减少重复扫描
- LRU 码率缓存（512 条上限）
- Protocol Buffers 二进制存储比 JSON 更高效

### 路径处理
- 支持绝对路径和相对路径两种格式
- URL 中的路径使用 `quote()` 编码
- 路径比较时使用 `os.path.normpath()` 标准化
- 安全检查：`_is_abs_path_allowed()` 和 `_is_path_safe()` 防止路径遍历攻击

### 转码系统
- **自动转码开关** `auto_transcode`：控制扫描时是否自动将非 MP4 视频加入转码队列（默认开启）
- 三个入队入口均受 `auto_transcode` 控制：空闲检测、增量扫描、后台转码线程
- 预转码和实时转码两种模式
- GPU 硬件加速（NVENC）显著提升速度
- 进度追踪使用 `-progress pipe:1` 解析
- 转码成功后自动生成缩略图
- 默认删除原文件避免列表重复

### 设置页面
- 5 个可折叠卡片：分类管理、高级设置、后台任务、数据管理、使用说明
- 自动扫描已合并到高级设置卡片（首个开关）
- 转码管理 + 元数据管理合并为「后台任务」卡片
- 清理缓存 + 隐藏视频管理合并为「数据管理」卡片
- 三个转码相关开关（自动转码、删除原文件、GPU转码）切换后实时保存
- 数值类高级设置需点击保存按钮生效

### 播放页面
- 快速渲染：服务端嵌入当前集缩略图
- 异步加载：`/api/play-info` 补全其他集信息
- 同分类合并：跨目录连续观看
- 方向自适应：横屏/竖屏自动布局
- PiP 画中画：自定义浮窗 + 浏览器原生 API 双模式
- **视频预加载**：进度 > 80% 自动预加载下一集（后台线程 + HTTP 缓存）

### 配置管理
- **所有配置项可通过设置页面修改**：端口、主机地址、缓存策略等
- **配置热更新**：修改配置后即时生效（部分配置需重启）
- **默认值 fallback**：所有配置项都有合理的默认值
- **硬编码已最小化**：服务器地址、分类名称等均可配置

### 自动扫描控制
- 自动扫描关闭时：`_file_watcher_loop` 跳过扫描轮次、`api_save_categories` 不启动后台增量更新
- 自动扫描开启时：watcher 活跃、保存分类自动增量更新
- **"重新扫描"按钮**通过 `force_scan: true` 强制触发，不受自动扫描开关影响

---

## 硬编码重构记录

**已完成重构**（2026-04-26）:
1. ✅ **服务器端口和主机**：从 `config.pb` 读取 `server_port` 和 `server_host`
2. ✅ **默认分类名称**：通过 `_get_default_category()` 函数动态获取
3. ✅ **未分类标签**：通过 `_get_uncategorized_label()` 函数动态获取
4. ✅ **API 配置接口**：新增端口和主机配置项的读写

**已完成重构**（2026-05-01）:
5. ✅ **JSON → Protocol Buffers**：所有缓存文件迁移为 `.pb` 二进制格式
6. ✅ **非阻塞扫描**：`get_all_series()` 将全量扫描移到锁外，防止阻塞事件循环
7. ✅ **异步保存分类**：`api_save_categories` 的增量扫描改为后台线程执行
8. ✅ **文件监控尊重开关**：`_file_watcher_loop` 每轮检查自动扫描状态
9. ✅ **调试代码清理**：`server.py` 中 188 个 `print()` 和 4 处 `traceback.print_exc()` 替换为 `logger = logging.getLogger("mycinema")` 的 `logger.info()`/`logger.exception()`；`pb_utils.py` 中 5 个 `print()` 替换为 `_pb_logger = logging.getLogger("mycinema.pb")` 的 `_pb_logger.info()`；`templates/video.html` 中 7 个 `console.log()` 已移除

**保留的合理硬编码**:
- ℹ️ **UI 尺寸**（54px, 360px等）：视觉设计常量，建议后续通过 CSS 变量统一管理
- ℹ️ **跳过目录列表**（static, templates等）：固定的系统目录，硬编码合理
- ℹ️ **安全地址白名单**（127.0.0.1等）：安全相关，硬编码合理

---

## 常见问题

### Q: 首页不显示内容怎么办？
A: 检查自动扫描是否开启，或点击"重新扫描"按钮触发强制扫描

### Q: 播放页面出现500错误？
A: 清除缓存后重新扫描，确保路径格式正确

### Q: 元数据管理显示不正确？
A: 检查 `video_meta_cache.pb` 文件是否存在

### Q: 添加目录后首页不更新？
A: 确保"重新扫描"按钮使用了 `force_scan: true`（已自动实现）

### Q: 关闭自动扫描后服务器仍在扫描？
A: 文件监控线程现在会尊重自动扫描开关，重启服务器后生效

**分辨率标签规则**:
- >= 4320 → 4K
- >= 2160 → 4K
- >= 1080 → 1080P
- >= 720 → 720P
- 其他 → 显示实际数值（如 640p）

---

## 更新记录

**2026-05-02**:
1. ✅ **移除 `video_base_dir` 字段**：从 `config.proto`、`config_pb2.py`、`pb_utils.py` 中删除已废弃的 `video_base_dir` 配置项（`VIDEO_BASE_DIR` 改为硬编码为 `BASE_DIR`）
2. ✅ **增量更新扫描缓存**：`_refresh_series_cache()` 和 `api_save_categories()` 改为增量合并策略，扫描期间旧视频仍可查看播放，新视频扫描完成后自动加入，消除缓存清空导致的空白期
3. ✅ **控制台日志输出**：`server.py` 添加 `StreamHandler`，扫描进度、转码状态等实时输出到控制台/终端，方便监控；同时保留文件日志到 `mycinema.log`
4. ✅ **元数据补全后自动修复缩略图**：`populate_all_video_meta()` 完成后自动调用 `_check_and_fix_missing_thumbnails()`，检查并生成缺失的缩略图文件，解决"元数据已补全但页面仍显示无缩略图"问题
5. ✅ **前端缩略图按需生成**：首页分类页面切换时，缩略图加载失败自动调用 `/api/cover/generate` 后台生成，生成成功后刷新图片，无需手动干预
6. ✅ **缩略图生成调试增强**：添加完整日志输出（路径解码、文件查找、生成结果），前端添加 console.log 调试信息，修复 URL 编码路径处理问题
7. ✅ **播放页面返回记住滚动位置**：点击返回按钮时保存滚动位置到 localStorage，返回首页后自动恢复到原来的位置，不再从顶部开始
8. ✅ **首页分类按管理顺序显示**：后端按分类管理顺序构建分组字典，首页显示的分类视频排序与分类管理中的排序一致（从上往下）
9. ✅ **分类页面点击视频时保存滚动位置**：在分类页面点击视频卡片时，使用事件捕获拦截，保存当前分类页面的滚动位置到 localStorage，确保从播放页面返回时能正确恢复
