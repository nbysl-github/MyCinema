# MyCinema

一个基于 **FastAPI + Jinja2** 的本地视频流媒体服务器，提供 B站风格（Bilibili Design）的 Web 界面，支持视频浏览、播放、转码和分类管理。

![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

---

## 特性

### 🎬 视频管理
- **智能扫描**：自动识别目录结构，区分扁平目录、混合目录、合集容器
- **分类管理**：支持多分类管理，关联不同目录
- **增量扫描**：快速检测新增/删除的视频，精确更新缓存
- **缩略图生成**：自动提取视频帧作为封面

### 🎨 B站风格界面
- **双模式适配**：自动识别系统亮/暗模式
- **响应式布局**：自适应窗口大小
- **无刷新切换**：播放页切集无需刷新页面
- **流畅动画**：精心设计的过渡动画

### ▶️ 播放体验
- **多种格式支持**：支持 MP4、MKV、AVI、MOV 等常见视频格式
- **自适应转码**：自动检测浏览器不兼容的编码并转码
- **外挂字幕**：支持 SRT、ASS 等字幕格式
- **进度记忆**：自动保存播放进度

### ⚡ 性能优化
- **异步架构**：扫描操作绝不阻塞 HTTP 请求
- **多层缓存**：内存缓存 + Protocol Buffers 持久化
- **增量更新**：只处理变化的视频文件
- **后台任务**：长时间操作在后台执行

---

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.12 / FastAPI / Uvicorn |
| 数据存储 | Protocol Buffers |
| 视频处理 | FFmpeg / OpenCV |
| 前端 | Jinja2 / Vanilla JavaScript / CSS |
| 图标 | Lucide Icons (内联 SVG) |

---

## 快速开始

### 环境要求

- Python 3.12+
- FFmpeg（可选，内置便携版）

### 安装依赖

```bash
pip install fastapi uvicorn opencv-python-python protobuf
```

### 启动服务

**Windows:**
```bash
run app.bat
```

**手动启动:**
```bash
python server.py
```

服务启动后访问 http://localhost:8000

---

## 项目结构

```
MyCinema/
├── server.py              # 核心后端（API路由、扫描逻辑、转码、缓存）
├── pb_utils.py            # Protocol Buffers 工具
├── config.pb              # 用户配置
├── categories.pb         # 分类配置
├── series_cache.pb        # 视频缓存
├── protos/                # Protocol Buffers 定义文件
├── templates/             # HTML 模板
│   ├── index.html         # 首页
│   ├── video.html         # 播放页
│   ├── detail.html        # 详情页
│   └── settings.html      # 设置页
├── static/
│   ├── icons.js           # Lucide 图标
│   └── css/bilibili.css   # B站风格样式
└── run app.bat            # Windows 启动脚本
```

---

## 配置说明

### 视频目录

在设置页面配置视频目录路径，系统会自动扫描并分类。

### 分类管理

- 创建多个分类（如：电影、剧集、MV 等）
- 为每个分类关联不同的目录
- 支持拖拽排序分类显示顺序

### 扫描选项

- **自动扫描**：检测到目录变化时自动增量更新
- **手动扫描**：通过分类管理卡片中的扫描按钮手动触发

---

## 目录类型说明

| 类型 | 说明 | 首页展示 |
|------|------|----------|
| 扁平目录 | 目录下直接包含视频文件 | 每个视频独立卡片 |
| 混合目录 | 同时包含视频和子目录 | 多个独立卡片 |
| 合集容器 | 只有子目录无直连视频 | 子目录各生成卡片 |

---

## 键盘快捷键

| 快捷键 | 功能 |
|--------|------|
| `Space` | 播放/暂停 |
| `←` / `→` | 快退/快进 5 秒 |
| `↑` / `↓` | 音量调节 |
| `F` | 全屏切换 |
| `M` | 静音切换 |

---

## License

MIT License
