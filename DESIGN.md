# MyCinema 设计系统

> 基于 B站（Bilibili）视觉风格，适配 Win11 亮/暗双模式，本地视频流媒体播放器。

---

## 1. 设计理念

| 原则 | 说明 |
|------|------|
| **Bilibili DNA** | 主色调粉色 `#FB7299`，年轻、清爽、高信息密度 |
| **双模式自适应** | 通过 `prefers-color-scheme: dark` 自动切换亮/暗主题 |
| **CSS Variables** | 所有颜色/阴影通过 CSS 自定义属性统一管理，服务端动态注入 B 站色值 |
| **Lucide Icons 全覆盖** | 零 emoji，全部使用内联 SVG 图标（自定义 `icons.js` 渲染引擎） |
| **方向感知** | 播放器自动识别竖向(9:16) / 横向(16:9)，布局自适应 |
| **SPA 式体验** | 播放页切集无刷新，收藏/搜索/筛选全客户端交互 |

---

## 2. 色彩系统

### 2.1 亮色模式（默认）

```css
--bg-body:       #f1f2f3;   /* 页面底色 */
--bg-nav:        #ffffff;   /* 导航栏 */
--bg-card:       #e8eaed;   /* 卡片/按钮底色 */
--bg-card-hover: #dcdfe3;   /* 卡片悬停 */
--text-primary:  #18191c;   /* 主文字 */
--text-secondary:#61666d;   /* 辅助文字 */
--border-color:  #e3e5e7;   /* 分割线 */
--divider:       #d8d8d8;
--brand-color:   #fb7299;   /* 品牌粉 */
--brand-light:   #ff85ad;
```

### 2.2 暗色模式（B站原色）

```css
--bg-body:       #18191C;   /* B站深黑背景 */
--bg-nav:        #232427;   /* 导航栏 */
--bg-card:       #2D2E32;   /* 卡片底色 */
--bg-card-hover: rgba(255,255,255,0.06);
--text-primary:  #FFFFFF;
--text-secondary: #9499A0;  /* B站灰 */
--border-color:  #3C3C3E;
--divider:       #3C3C3E;
--brand-color:   #FB7299;   /* 品牌粉不变 */
--brand-light:   #FF4F7D;   /* Hover高亮粉 */
```

### 2.3 功能色

| 用途 | 色值 | 说明 |
|------|------|------|
| 成功/正常 | `#00B18E` / `#4ECDC4` | FFmpeg 正常、NVENC 可用 |
| 警告 | `#FFB027` | - |
| 错误/危险 | `#F45A9D` / `#E74C3C` | FFmpeg 异常、删除按钮、转码失败 |
| 链接/信息 | `#3498DB` | CPU 信息 |
| GPU | `#E74C3C` | GPU 信息 |
| 内存 | `#9B59B6` | 内存信息 |
| 磁盘 | `#E67E22` | 磁盘信息 |
| 收藏高亮 | `#FFD700` | 金色星标 |
| 徽章背景 | `rgba(0,0,0,0.55~0.72)` | 封面上浮标签 |

---

## 3. 排版

### 字体

```
font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
```

- 中文：PingFang SC → Microsoft YaHei 回退
- 英文/数字：系统 sans-serif
- 无衬线体

### 字阶

| 名称 | 大小 | 用途 |
|------|------|------|
| title-1 | 24px | 页面主标题 |
| page-title | 22px | 设置页标题 |
| section-title | 18px | 分类区块标题 |
| nav-title | 15~16px | 导航标题 |
| body / card-title | 14~15px | 卡片标题、正文 |
| caption / meta | 12~13px | 元信息、集数、文件大小 |
| tag / badge | 10~11px | 标签、分辨率标识、集数徽章 |
| ctrl-res | 11px | 控制栏分辨率 |

### 字重

- regular (400): 正文
- medium (500): 按钮、卡片标题
- semibold (600): 区块标题、分类 tab active
- bold (700): Logo

---

## 4. 布局

### 全局结构

```
┌──────────────────────────────────────┐
│  top-nav (fixed, z-1000, h=56~64px) │ ← 固定顶栏
├──────────────────────────────────────┤
│                                      │
│         main-content                 │ ← margin-top: 76~84px
│         padding: 16~24px             │    max-width: 1000~1160px
│                                      │
└──────────────────────────────────────┘
```

### 首页布局

```
top-nav:
  [Logo] [搜索框(flex-1)] [分类胶囊] [排序] [收藏⭐] [设置⚙]

main-content:
  ┌─ 分类区块 ──────────────────────────┐
  │ 📺 分类名                    N 个    │  section-title-bar + count badge
  │ ════════                        (color divider)
  │ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐│  series-grid (auto-fill, minmax)
  │ │封面  │ │封面  │ │封面  │ │封面  ││  竖向: 160px min / 横向: 240px min
  │ │9:16  │ │9:16  │ │9:16  │ │9:16  ││  gap: 20px 16px
  │ │N集   │ │N集   │ │N集   │ │N集   ││
  │ │标题  │ │标题  │ │标题  │ │标题  ││
  │ └──────┘ └──────┘ └──────┘ └──────┘│
  └─────────────────────────────────────┘
```

### 播放页布局

```
body.landscape (横向视频):
┌──────────────────────────────────────────┐
│ top-nav: [Logo] [剧名 - 第N集] [返回列表] │
├────────────────────────┬─────────────────┤
│                        │                 │
│    player-wrapper      │  playlist-      │  flex + gap: 20px
│    (16:9, 圆角8px)     │  sidebar        │  align-items: center
│    [自定义控制栏]       │  (剧集列表)     │
│                        │                 │
├────────────────────────┴─────────────────┤
│ video-info-bar (信息栏)                  │  标题 + 集数 + 分辨率 + 大小...
└─────────────────────────────────────────┘

body.portrait (竖向视频, 默认):
同上结构，player-wrapper aspect-ratio 9:16
```

### 设置页布局

```
top-nav: [Logo] [设置] [返回首页]

server-status-bar (grid, 5列):
  [版本] [运行时间] [FFmpeg] [NVENC] [CPU] [GPU] [内存] [磁盘]
  ↑ 第一行5个，第二行3个

settings-card × N (可折叠手风琴):
  ┌─ 🎬 转码管理 ────────────── ▼ ─┐
  │  统计: X个任务 / Y等待中     │
  │  ┌──────────────────────┐   │
  │  │ 任务队列...           │   │
  │  └──────────────────────┘   │
  └─────────────────────────────┘
```

### 间距规范

| 属性 | 值 |
|------|-----|
| 导航栏高度 | 56px（设置/详情）/ 64px（首页） |
| 内容区上边距 | 76px（设置/详情）/ 84px（首页） |
| 内容区左右边距 | 20~24px |
| 卡片间距 | gap: 12~20px |
| 区块间距 | margin-bottom: 24~36px |
| 卡片内边距 | padding: 12px |
| 搜索框高度 | 40px |

---

## 5. 组件

### 5.1 顶部导航 `.top-nav`

- 固定定位 `position: fixed; top: 0; z-index: 1000`
- 背景 `var(--bg-nav)` + 底部细线/阴影
- 高度：首页 64px，其余 56px
- Logo：品牌粉色、700 字重、无下划线
- 搜索框：圆角 8px、无边框、聚焦时品牌粉色描边 + 下拉建议

### 5.2 视频卡片 `.series-card`

```
┌──────────────────────┐
│ [📭占位图 / 封面图片] │  aspect-ratio: 9/16 或 16/9
│ ┌─720P─┐      [N集]  │  左上: res-tag, 右下: episode-badge
│ [♡]          [ℹ][×]  │  hover 显示: fav/detail/delete 按钮
├──────────────────────┤
│ 标题 (最多2行省略)     │
│ 全N集                │  meta 灰色小字
└──────────────────────┘
```

- hover: `translateY(-4px)` 上浮动画
- 封面圆角 6px，投影 `var(--shadow)`
- 操作按钮：圆形 24×24，半透明黑色底，hover 显现
  - 收藏 ⭐ 左下角 → favorited 金色
  - 详情 ℹ 右上角 → hover 粉色
  - 删除 × 右上角 → hover 红色

### 5.3 播放器 `.player-wrapper`

- 黑底、圆角 8px、大投影
- 自定义控制栏覆盖在底部（非原生 controls）
- 控制栏元素从左到右：

```
[▶播放] [⏮上一集] [00:00 / ========进度条======== / 00:00] [⏭下一集]
                                                    [🔊音量滑块] [1080P] [1.0x倍速] [字幕] [⛶全屏]
```

- 控制 bar: `linear-gradient(transparent, rgba(0,0,0,0.7))`
- hover / 触摸时显示（opacity transition 0.2s）
- 点击视频区域切换暂停/播放（排除控件区）
- 进度条可拖拽，hover 显示时间预览 tooltip
- 音量：竖向滑块，悬停展开
- 倍速菜单：0.5x ~ 2.0x 七档，点击弹出
- 外挂字幕：自动扫描 .srt/.ass/.vtt 文件
- 分辨率标签 `.ctrl-res`: 11px 加粗白色，音量图标左侧

### 5.4 剧集侧边栏 `.playlist-sidebar`

- 竖向视频: 缩略图 `aspect-ratio: 9/16`, height=54px
- 横向视频(body.landscape): 缩略图 `aspect-ratio: 16/9`, height=54px
- 每项: `[缩略图] [集号·文件名]`
- 当前集: `.active` 高亮
- 支持搜索过滤
- 同分类合并时显示目录分隔标签

### 5.5 按钮 `.btn`

| 类型 | 背景 | 文字 | 用途 |
|------|------|------|------|
| primary | `var(--brand-color)` | 白色 | 主要操作（保存、确认） |
| success | `#00B18E` | 白色 | 成功状态 |
| danger | `#F45A9D` | 白色 | 删除、清空等破坏性操作 |
| secondary | `var(--bg-card)` | 主色文字 | 次要操作（浏览、导出） |
| disabled | `#9aa0a6` | 白色 | 不可用状态 |

通用属性：
- 圆角 6px
- 内边距 8px 18px（sm: 4px 12px）
- 字号 14px，字重 500
- hover: `brightness(1.1)` 提亮
- 图标+文字组合: `gap: 6px`, inline-flex
- Lucide 图标尺寸: 16px（按钮内）/ 14px（紧凑场景）

### 5.6 分类 Tab `.cat-tab` / `.nav-cat-btn`

- 圆角胶囊形: `border-radius: 16~20px`
- 内边距: `6px 14~16px`
- 字号: 13px
- 默认: `var(--bg-card)` 底色
- active: `var(--brand-color)` 白字 + 600 字重
- hover: `scale(1.05)` 微缩放
- 支持 lucide 图标前缀

### 5.7 设置卡片 `.settings-card`

- 圆角 10px，溢出隐藏
- 折叠式手风琴: `.card-header` 可点击展开/收起
- header: 图标 + 标题 + 折叠箭头
- 表单组: label + input + unit 说明
- Toggle 开关: 自定义 CSS 实现
- 服务器状态栏: `grid-template-columns: repeat(5, 1fr)` 五列网格

### 5.8 收藏面板 `.fav-panel`

- 固定定位右上角: `top: 64px; right: 20px`
- 宽度 320px，最大高度 500px 可滚动
- 圆角 12px，强投影
- 每项: `[缩略图] [名称] [元信息] [删除按钮]`
- 遮罩层点击关闭

### 5.9 空状态 `.empty-state`

- 居中大图标 (lucide `inbox`, opacity 0.4)
- 灰色提示文案
- 内边距 80px 上下

---

## 6. 圆角与阴影

### 圆角半径

| 元素 | 半径 |
|------|------|
| 导航栏底部 / 搜索框 | 8px |
| 播放器容器 | 8px |
| 设置卡片 | 10px |
| 视频卡片 / 封面 | 6px |
| 按钮 / 返回按钮 | 6px / 4px |
| 分类 Tab | 16~20px（胶囊） |
| 徽章 / 标签 | 3px / 10~12px |
| 操作按钮（圆形） | 50% |
| 收藏面板 / 下拉菜单 | 12px / 8px |

### 阴影

```css
/* 亮色 */
--shadow-card:  0 2px 12px rgba(0,0,0,0.10);
--shadow-nav:    0 2px 8px  rgba(0,0,0,0.08);
/* 暗色 */
--shadow-card:  0 2px 12px rgba(0,0,0,0.30);
--shadow-nav:    0 2px 8px  rgba(0,0,0,0.25);
/* 播放器专用 */
box-shadow: 0 4px 24px rgba(0,0,0,0.35);
/* 收藏面板 */
box-shadow: 0 8px 32px rgba(0,0,0,0.3);
```

---

## 7. 交互规范

### 动画时长

| 场景 | 时长 | 缓动 |
|------|------|------|
| hover 过渡 | 0.15~0.2s | ease |
| 卡片悬浮 | 0.25s | ease (translateY) |
| 控制栏显隐 | 0.2s | opacity |
| 导航搜索聚焦 | 0.3s | all |
| 设置按钮旋转 | 0.2s | transform (30deg) |
| 面板展开收起 | 由内容决定 | height/max-height |

### 交互行为

- **Hover**: 亮度变化 + 轻微缩放 (`scale(1.02~1.15)`) — B站经典微交互
- **按钮**: hover 提亮 `brightness(1.1)` / `brightness(0.95)` (secondary)
- **卡片**: hover 上浮 `translateY(-4px)`
- **设置图标**: hover 旋转 30°
- **操作按钮**: 默认透明(opacity:0)，卡片 hover 时显现
- **搜索建议**: 输入防抖 150ms，失焦延迟 200ms 关闭
- **分类筛选**: 点击已选中项取消筛选（toggle），URL 同步更新
- **切集**: SPA 无刷新，history.pushState 更新 URL，自动滚动到当前集
- **键盘快捷键**: 空格(播放/暂停)、←→(快退/快进)、↑↓(音量)、[] (上下集)、F (全屏)

---

## 8. 图标系统

### 技术方案

- **库**: Lucide Icons（开源 SVG 图标集）
- **渲染**: 自定义 `icons.js` — 扫描 `i[data-lucide]` 替换为内联 SVG
- **调用**: `initIcons()` 函数（页面加载后 + DOM 动态变更后）
- **动态更新**: `element.innerHTML = '<i data-lucide="xxx"></i>'; initIcons();`

### 使用方式

```html
<!-- 静态 HTML -->
<i data-lucide="play"></i>

<!-- JS 动态 -->
btn.innerHTML = '<i data-lucide="pause"></i>';
initIcons();
```

### 项目使用图标清单

| 图标名 | 用途 | 所在页面 |
|--------|------|----------|
| play / pause | 播放/暂停 | video.html |
| skip-back / skip-forward | 上一集/下一集 | video.html |
| fullscreen | 全屏 | video.html |
| volume-2 / volume-x | 音量开/静音 | video.html |
| subtitles | 字幕 | video.html |
| keyboard | 快捷键面板 | video.html |
| home | 返回列表 | video, detail |
| info | 详情按钮 | index, video |
| heart | 收藏 | index, settings |
| search | 搜索 | index |
| settings | 设置入口 | index |
| trash / trash-2 | 删除/清理 | index, settings |
| inbox | 空状态占位 | index |
| film | 视频/封面占位 | index, detail, video |
| tv | 默认分类图标 | server.py (数据) |
| folder / folder-open / folder-cog | 目录/分类管理 | index, detail, settings |
| grid | 返回列表 | detail |
| plus | 新建分类 | settings |
| download / upload | 导出/导入 | settings |
| rotate-cw | 重新扫描 | settings |
| check | 保存成功 | settings |
| sliders-horizontal | 高级设置/元数据管理 | settings |
| cpu | CPU 信息 | settings |
| gpu | GPU 信息 | settings |
| memory-stick | 内存信息 | settings |
| hard-drive | 磁盘信息 | settings |
| cable | 运行时间 | settings |
| book-check | 版本号 | settings |
| zap | NVENC 状态 | settings |
| loader | 加载中 | settings |
| monitor | 方向/显示器信息 | detail, settings |
| list / hard-drive-download | 列表/总大小 | detail |
| clock | 时间 | settings |
| gamepad-2 / clapperboard / popcorn | （预留娱乐图标） | icons.js |
| star / sparkles / flame / gem | （预留装饰图标） | icons.js |
| image / camera / video | （预留媒体图标） | icons.js |
| disc / disc-2 / radio / podcast | （预留音频图标） | icons.js |
| music / mic / headphones / speaker | （预留音频图标） | icons.js |
| eye-off / rotate-ccw | （预留工具图标） | icons.js |
| ruler / calendar | （预留工具图标） | icons.js |

### 图标尺寸规范

| 场景 | 尺寸 |
|------|------|
| 按钮内联 | width/height: 16px (!important) |
| 导航栏 | 18~20px |
| 卡片操作按钮 | 14px |
| 区块标题旁 | 22px |
| 分类导航前缀 | 16px |
| 空状态大图标 | 36~56px |
| 收藏面板 | 24px (icon), 18px (close/del), 16px (thumb icon) |
| 服务器状态栏 | 14px (!important) |
| 播放器控制栏 | 继承 .ctrl-btn font-size: 18px |

---

## 9. 响应式

### 断点

| 断点 | 行为 |
|------|------|
| ≤ 600px (手机) | 卡片列宽缩小至 120px；播放器进入全屏模式；浮动返回按钮显示 |
| > 600px | 默认桌面布局 |

### 移动端特殊处理

- `.mobile-back-btn`: 浮动左上角返回按钮
- 播放页: 强制全屏式体验
- 网格: `minmax()` 自动调整列数

---

## 10. 页面结构速查

### 首页 `/`
```
Nav → MainContent → (CategorySections / FlatGrid / EmptyState) → Scripts
```
- 搜索建议（实时防抖）
- 分类筛选（Tab 切换 + URL 同步）
- 收藏功能（localStorage）
- 卡片操作（详情/收藏/删除）

### 播放页 `/play/{path}/{ep}`
```
Nav → MainLayout → PlayerSection + PlaylistSidebar + InfoBar → Scripts
```
- 自定义视频控件（播放/暂停/进度/音量/倍速/字幕/全屏）
- SPA 切集（无刷新）
- 键盘快捷键
- AJAX 异步加载详细信息
- 方向自适应布局

### 详情页 `/detail/{path}`
```
Nav → MainContent → (Cover + MetaBar + OverviewTable + EpisodeTable + Lightbox) → Scripts
```
- 视频概览表
- 详细剧集表格（AJAX 补全）
- 缩略图灯箱

### 设置页 `/settings`
```
Nav → MainContent → ServerStatusBar + SettingsCards → Scripts
```
- 手风琴折叠卡片
- 实时服务器监控（5s/10s 双频率刷新）
- 分类 CRUD + 拖拽排序
- 配置热更新

---

## 11. UI Style Prompt（供 AI 参考）

> **Style**: Modern Bilibili-inspired media streaming interface. Dark-first dual-mode theme (auto light/dark via prefers-color-scheme). Pink primary (#FB7299). Clean rounded cards with subtle shadows and hover lift animations. Custom video player with gradient control overlay. Lucide SVG icons throughout (zero emojis). High information density with clear visual hierarchy. Chinese font stack (PingFang SC > Microsoft YaHei). CSS variables for theming. Responsive grid layout. Smooth micro-interactions (scale, fade, slide).
