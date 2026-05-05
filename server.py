import logging
import sys

logger = logging.getLogger("mycinema")
# 控制台日志处理器（实时输出扫描进度等）
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter('[%(asctime)s] %(name)s: %(message)s', datefmt='%H:%M:%S'))
logger.addHandler(_console_handler)
logger.setLevel(logging.INFO)
# pyright: reportMissingTypeArgument=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAny=false, reportUnknownLambdaType=false, reportAssignmentType=false, reportConstantRedefinition=false, reportArgumentType=false, reportUnusedCallResult=false, reportMissingModuleSource=false, reportPossiblyUnboundVariable=false, reportOptionalMemberAccess=false
from collections import OrderedDict
from typing import cast
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, FileResponse, Response, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import os
import sys
import io
import glob
import cv2
import numpy as np
import re
import time

def _natural_sort_key(s: str) -> list:
    """自然排序：数字部分按数值排序，使 2.mp4 排在 10.mp4 前面"""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', s)]
import threading
import json
import pb_utils
import asyncio
import subprocess
from urllib.parse import unquote, quote
from jinja2 import Environment, FileSystemLoader

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    psutil = None
    try:
        import subprocess, sys
        logger.info("[提示] 正在自动安装 psutil ...")
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'psutil', '-q'])
        import psutil
        PSUTIL_AVAILABLE = True
        logger.info("[成功] psutil 已安装")
    except Exception as e:
        logger.info(f"[警告] psutil 安装失败: {e}，部分功能将不可用")
        psutil = None

if sys.platform == 'win32':
    cast(io.TextIOWrapper, sys.stdout).reconfigure(encoding='utf-8', errors='replace')
    cast(io.TextIOWrapper, sys.stderr).reconfigure(encoding='utf-8', errors='replace')

# ====================== 全局配置 ======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.pb")
CATEGORIES_FILE = os.path.join(BASE_DIR, "categories.pb")
THUMBNAIL_CACHE_FILE = os.path.join(BASE_DIR, "thumbnail_cache.pb")
SERIES_CACHE_FILE = os.path.join(BASE_DIR, "series_cache.pb")
ALLOWED_EXTENSIONS = {
    '.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv',
    '.webm', '.m4v', '.mpg', '.mpeg', '.m2ts', '.ts',
    '.3gp', '.asf', '.rm', '.rmvb', '.vob', '.ogv',
    '.f4v', '.divx', '.xvid',
}

TRANSCODE_FORMATS = {'.mkv', '.avi', '.mov', '.flv', '.wmv', '.webm', '.m4v',
                     '.mpg', '.mpeg', '.m2ts', '.ts', '.rm', '.rmvb',
                     '.vob', '.3gp', '.asf', '.f4v', '.divx', '.xvid', '.ogv'}

_pending_transcode = {}  # 待转码视频字典 {filepath: series_id}
_pending_transcode_lock = threading.Lock()

# JSON -> Protobuf 迁移（首次启动时自动执行）
pb_utils.run_migration(BASE_DIR)

# ====================== 性能优化：内存缓存层 ======================
_series_list_cache: list[dict] | None = None       # get_all_series 结果缓存
_series_list_cache_time = 0     # 缓存时间戳
_series_list_cache_lock = threading.Lock()  # 保护 _series_list_cache 的读写
_series_scan_lock = threading.Lock()  # 防止并发全量扫描
SERIES_CACHE_TTL = 300           # 缓存有效期（秒），启动后从 _config 更新
_categories_cache: list[dict] | None = None  # load_categories 结果缓存（初始为 None）
_categories_cache_mtime = 0     # 分类文件修改时间
_bitrate_cache: OrderedDict = OrderedDict()  # 视频码率缓存 {video_path: bitrate_bps}，LRU 上限 512
_BITRATE_CACHE_MAX = 512

# ====================== 多级缓存系统（L1 内存 → L2 磁盘 → L3 源文件）======================
_video_content_cache: dict = {}  # L1: 内存缓存 {url_key: {"data": bytes, "size": int, "time": float}}
_video_content_cache_lock = threading.Lock()
VIDEO_CONTENT_CACHE_MAX_SIZE = 50 * 1024 * 1024  # 50MB 内存缓存上限
VIDEO_CONTENT_CACHE_TTL = 600  # 10 分钟 TTL

# 预加载队列
_prefetch_queue: list[tuple[str, str]] = []  # (url_key, filepath) 预加载任务队列
_prefetch_lock = threading.Lock()
_PREFETCH_WORKER_ACTIVE = False

# CDN 缓存头配置
CDN_CACHE_CONTROL_MAX_AGE = 86400  # 24 小时最大缓存时间
CDN_ETAG_ENABLED = True  # 启用 ETag

_ffmpeg_available = None

# MIME 类型映射表（模块级常量，避免每次请求重建）
MIME_MAP = {
    '.mp4': 'video/mp4',
    '.mkv': 'video/x-matroska',
    '.avi': 'video/x-msvideo',
    '.mov': 'video/quicktime',
    '.flv': 'video/x-flv',
    '.webm': 'video/webm',
    '.m4v': 'video/mp4',
    '.mpg': 'video/mpeg',
    '.mpeg': 'video/mpeg',
    '.m2ts': 'video/mp2t',
    '.ts': 'video/mp2t',
    '.3gp': 'video/3gpp',
    '.asf': 'video/x-ms-asf',
    '.rm': 'application/vnd.rn-realmedia',
    '.rmvb': 'application/vnd.rn-realmedia-vbr',
    '.vob': 'video/mpeg',
    '.ogv': 'video/ogg',
    '.f4v': 'video/x-f4v',
    '.divx': 'video/x-msvideo',
    '.xvid': 'video/x-msvideo',
}

# 字幕 MIME 类型映射
SUBTITLE_MIME_MAP = {
    '.srt': 'text/srt',
    '.ass': 'text/x-ass',
    '.ssa': 'text/x-ssa',
    '.vtt': 'text/vtt',
    '.sup': 'application/octet-stream',
    '.sub': 'application/octet-stream',
}



def _is_path_safe(base_dir, filepath):
    """检查路径是否在指定基础目录下，防止路径遍历攻击"""
    normalized = os.path.normpath(os.path.abspath(filepath))
    normalized_base = os.path.normpath(os.path.abspath(base_dir))
    return normalized.startswith(normalized_base + os.sep) or normalized == normalized_base


# ====================== 多级缓存系统函数 ======================

def _get_content_cache_key(filepath: str, start: int = 0, end: int = -1) -> str:
    """生成内容缓存的 key"""
    return f"{filepath}:{start}-{end}"


def _cache_get_l1(filepath: str, start: int = 0, end: int = -1):
    """L1 缓存：内存缓存读取"""
    key = _get_content_cache_key(filepath, start, end)
    with _video_content_cache_lock:
        entry = _video_content_cache.get(key)
        if entry and (time.time() - entry["time"]) < VIDEO_CONTENT_CACHE_TTL:
            return entry["data"]
        elif entry:
            # TTL 过期，删除
            _video_content_cache.pop(key, None)
    return None


def _cache_set_l1(filepath: str, data: bytes, start: int = 0, end: int = -1):
    """L1 缓存：内存缓存写入（带 LRU 淘汰）"""
    key = _get_content_cache_key(filepath, start, end)
    with _video_content_cache_lock:
        # 如果超出大小限制，淘汰最旧的数据
        while _video_content_cache and sum(e["size"] for e in _video_content_cache.values()) > VIDEO_CONTENT_CACHE_MAX_SIZE:
            oldest_key = min(_video_content_cache, key=lambda k: _video_content_cache[k]["time"])
            removed_size = _video_content_cache.pop(oldest_key)["size"]
            logger.info(f"[缓存 L1] 淘汰旧数据: {oldest_key} (释放 {removed_size / 1024 / 1024:.1f}MB)")
        _video_content_cache[key] = {
            "data": data,
            "size": len(data),
            "time": time.time()
        }


def _start_prefetch_worker():
    """启动预加载工作线程"""
    global _PREFETCH_WORKER_ACTIVE
    
    def prefetch_worker():
        _PREFETCH_WORKER_ACTIVE = True
        while _PREFETCH_WORKER_ACTIVE:
            try:
                # 从队列获取任务
                with _prefetch_lock:
                    if not _prefetch_queue:
                        time.sleep(2)
                        continue
                    task = _prefetch_queue.pop(0)
                
                url_key, filepath = task
                logger.info(f"[预加载] 开始预加载: {os.path.basename(filepath)}")
                # 读取文件内容
                with open(filepath, 'rb') as f:
                    data = f.read()
                
                # 存入 L1 缓存
                _cache_set_l1(filepath, data)
                logger.info(f"[预加载] 完成: {os.path.basename(filepath)} ({len(data) / 1024 / 1024:.1f}MB)")
            except Exception as e:
                logger.info(f"[预加载] 失败: {e}")
    worker_thread = threading.Thread(target=prefetch_worker, daemon=True)
    worker_thread.start()
    logger.info("[预加载] 工作线程已启动")
def _prefetch_video(filepath: str, url_key: str):
    """添加预加载任务到队列"""
    with _prefetch_lock:
        if len(_prefetch_queue) < 10:  # 预加载队列最大 10 个任务
            _prefetch_queue.append((url_key, filepath))
            logger.info(f"[预加载] 已加入队列: {os.path.basename(filepath)}")
def _generate_etag(filepath: str, last_modified: float, size: int) -> str:
    """生成 ETag（用于 CDN 缓存验证）"""
    import hashlib
    etag_data = f"{filepath}:{last_modified}:{size}"
    return '"' + hashlib.md5(etag_data.encode()).hexdigest()[:16] + '"'


def _parse_if_none_match(request_headers: dict) -> str | None:
    """解析 If-None-Match 请求头"""
    if_none_match = request_headers.get('if-none-match', '')
    if if_none_match:
        # 支持多个 ETag
        etags = [e.strip().strip('"') for e in if_none_match.split(',')]
        return etags[0] if etags else None
    return None


def _is_abs_path_allowed(filepath):
    """检查绝对路径是否在允许的目录范围内（使用缓存避免每次读文件）"""
    try:
        cats = _categories_cache if _categories_cache is not None else []
    except (TypeError, KeyError):
        try:
            data = pb_utils.read_categories(CATEGORIES_FILE)
            cats = data or []
        except (OSError, Exception):
            cats = []
    if not cats or sum(len(cat.get('dirs', [])) for cat in cats) == 0:
        return True
    # 使用 realpath 解析符号链接并标准化路径
    real_path = os.path.realpath(filepath)
    normalized = os.path.normpath(os.path.abspath(real_path))
    dirs = {os.path.normpath(os.path.abspath(BASE_DIR))}
    for cat in cats:
        for d in cat.get('dirs', []):
            d_str: str = str(d)
            full = d_str if os.path.isabs(d_str) else os.path.join(VIDEO_BASE_DIR, d_str)
            # 对允许的目录也使用 realpath 标准化
            dirs.add(os.path.normpath(os.path.abspath(os.path.realpath(full))))
    for allowed in dirs:
        if normalized.startswith(allowed + os.sep) or normalized == allowed:
            return True
    return False

def _video_needs_transcode(filepath):
    """检查视频是否需要转码且尚未完成"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in TRANSCODE_FORMATS:
        # .mp4 文件还需检查编码格式（如 MPEG-4 Part 2 浏览器无法播放）
        if ext == '.mp4' and not _is_mp4_browser_playable(filepath):
            return True
        return False
    mp4_path = os.path.splitext(filepath)[0] + '.mp4'
    return not os.path.exists(mp4_path)


_BROWSER_PLAYABLE_CODECS = {'h264', 'vp9', 'av1', 'mpeg2video'}
# 注意：hevc/h265 虽然部分浏览器支持，但 Windows 上硬件解码兼容性差，需要转码
_CODEC_CHECK_CACHE_MAX = 1000
_codec_check_cache = OrderedDict()
_codec_check_cache_lock = threading.Lock()


def _is_mp4_browser_playable(filepath):
    """检测 .mp4 文件的视频编码是否为浏览器可直接播放的格式"""
    mtime = 0
    try:
        mtime = int(os.path.getmtime(filepath))
    except OSError:
        pass

    with _codec_check_cache_lock:
        cached = _codec_check_cache.get(filepath)
        if cached and cached[0] == mtime:
            _codec_check_cache.move_to_end(filepath)
            return cached[1]

    # 默认可播放：检测失败时不应阻止正常视频显示
    result = True
    try:
        proc = subprocess.run(
            [os.path.join(BASE_DIR, 'ffmpeg', 'ffprobe.exe'), '-v', 'quiet', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_name', '-of', 'csv=p=0',
             filepath],
            capture_output=True, text=True, timeout=10
        )
        if proc.returncode == 0:
            codec = proc.stdout.strip().lower()
            if codec and codec not in _BROWSER_PLAYABLE_CODECS:
                result = False
                logger.info(f"[编码检测] {os.path.basename(filepath)} 编码={codec}，需要转码")
    except Exception as e:
        logger.info(f"[编码检测] 检测失败(默认可播放): {os.path.basename(filepath)}, 错误: {e}")
    with _codec_check_cache_lock:
        _codec_check_cache[filepath] = (mtime, result)
        _codec_check_cache.move_to_end(filepath)
        if len(_codec_check_cache) > _CODEC_CHECK_CACHE_MAX:
            _codec_check_cache.popitem(last=False)
    return result

def _mark_video_pending_transcode(filepath, series_id):
    """将视频标记为待转码"""
    _add_pending_transcode(filepath, series_id)


def _add_pending_transcode(filepath, series_id):
    """将视频标记为待转码"""
    with _pending_transcode_lock:
        if filepath not in _pending_transcode:
            _pending_transcode[filepath] = series_id

def _remove_pending_transcode(filepath):
    """移除待转码标记"""
    with _pending_transcode_lock:
        _pending_transcode.pop(filepath, None)

def _filter_pending(data):
    """过滤掉需要转码但尚未完成的视频，合集内全部待转码则隐藏整个合集"""
    if isinstance(data, list):
        return [item for item in data if not item.get('needs_transcode', False)]
    if not isinstance(data, dict):
        return data
    if data.get('needs_transcode', False):
        return None
    videos = data.get('videos', [])
    if videos:
        playable = [v for v in videos if not v.get('needs_transcode', False)]
        if not playable:
            return None
        data = dict(data)
        data['videos'] = playable
        data['episode_count'] = len(playable)
        data['total_episodes'] = max(data.get('total_episodes', 0), len(playable))
    return data

def _update_series_cache_for_video(video_path):
    """
    定向更新转码文件所在目录的缓存。
    
    为什么需要这个函数：
    - 转码完成后，需要更新缓存以反映新的文件状态
    - 避免清空全量缓存，只更新受影响的目录条目
    - 提高性能，减少不必要的全量扫描
    """
    global _series_list_cache, _series_list_cache_time
    video_dir = os.path.dirname(video_path)

    # 先从磁盘重新加载分类配置，确保拿到最新数据
    cats = load_categories()

    # 查找该目录所属的分类信息
    cat_info = None
    for cat in cats:
        dirs = cat.get('dirs') or []
        for d in dirs:
            full_d = d if os.path.isabs(d) else os.path.join(VIDEO_BASE_DIR, d)
            if os.path.normpath(full_d) == os.path.normpath(video_dir):
                cat_info = {**_get_default_category(), "dirs": cat.get('dirs', [])}
                break
        if cat_info:
            break

    # 扫描该目录（force_series=True 防止扁平目录被误判）
    logger.info(f"[预转码]   重新扫描目录: {video_dir}")
    results = scan_dir_for_series(video_dir, cat_info, force_series=True)
    if results:
        filtered = _filter_pending(results)
        if filtered:
            # 同时更新内存缓存和磁盘文件
            with _series_cache_lock:
                update_series_cache_entry(video_dir, results)
                # 同步清除列表缓存，强制下次请求重新扫描
                _series_list_cache = None
                _series_list_cache_time = 0
            save_series_cache()
            logger.info(f"[预转码]   缓存已更新并持久化: {video_dir}")
            return

    # 目录为空或无有效视频，删除缓存条目
    with _series_cache_lock:
        _series_cache.pop(video_dir, None)
        _series_list_cache = None
        _series_list_cache_time = 0
    save_series_cache()
    logger.info(f"[预转码]   目录无有效视频，已删除缓存: {video_dir}")
def check_ffmpeg():
    """检查系统是否安装了 ffmpeg"""
    global _ffmpeg_available
    if _ffmpeg_available is not None:
        return _ffmpeg_available

    search_paths = ['ffmpeg']
    local_ffmpeg = os.path.join(BASE_DIR, 'ffmpeg', 'ffmpeg.exe')
    if os.path.exists(local_ffmpeg):
        search_paths.insert(0, local_ffmpeg)

    for cmd in search_paths:
        try:
            result = subprocess.run([cmd, '-version'], capture_output=True, timeout=5)
            if result.returncode == 0:
                _ffmpeg_available = True
                logger.info(f"[FFmpeg] 已检测到: {cmd}")
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    _ffmpeg_available = False
    logger.info("[FFmpeg] 未找到 FFmpeg，部分格式可能无法播放")
    return False


def get_ffmpeg_cmd():
    """获取 ffmpeg 可执行文件路径"""
    local_ffmpeg = os.path.join(BASE_DIR, 'ffmpeg', 'ffmpeg.exe')
    if os.path.exists(local_ffmpeg):
        return local_ffmpeg
    return 'ffmpeg'


# ========== GPU (NVENC) 硬件转码支持 ==========
_nvenc_available: bool | None = None  # None=未检测, True/False=已检测

def _detect_nvenc() -> bool:
    """检测 FFmpeg 是否支持 NVENC（NVIDIA 硬件编码）"""
    global _nvenc_available
    if _nvenc_available is not None:
        return _nvenc_available
    try:
        cmd = get_ffmpeg_cmd()
        result = subprocess.run(
            [cmd, '-hide_banner', '-encoders', '2>/dev/null'],
            capture_output=True, text=True, timeout=10
        )
        encoders = result.stdout + result.stderr
        _nvenc_available = ('h264_nvenc' in encoders) or ('hevc_nvenc' in encoders)
        logger.info(f"[GPU] NVENC 检测结果: {'支持' if _nvenc_available else '不支持'}")
        return _nvenc_available
    except Exception as e:
        logger.info(f"[GPU] NVENC 检测失败: {e}")
        _nvenc_available = False
        return False


def is_gpu_transcode_enabled() -> bool:
    """判断是否启用 GPU 转码（需要配置开启 + NVENC 可用）"""
    return bool(_config.get("gpu_transcode", False)) and _detect_nvenc()


def get_video_duration(video_path):
    """使用 ffprobe 获取视频时长（秒），失败返回 None（带 mtime 缓存）"""
    cached = _get_meta_cached(video_path)
    if cached and 'duration' in cached:
        dur = cached['duration']
        return dur if dur and dur > 0 else None

    ffmpeg_dir = os.path.join(BASE_DIR, 'ffmpeg')
    candidates = [
        os.path.join(ffmpeg_dir, 'ffprobe.exe'),
        'ffprobe',
    ]
    dur = None
    for cmd in candidates:
        if not os.path.exists(cmd) and cmd == candidates[0]:
            continue
        try:
            result = subprocess.run(
                [cmd, '-v', 'error', '-show_entries',
                 'format=duration:stream=duration,codec_type',
                 '-of', 'csv=p=0', '-select_streams', 'v:0',
                 video_path],
                capture_output=True, timeout=15
            )
            if result.returncode == 0:
                text = result.stdout.decode().strip()
                parts = text.split(',')
                for p in parts:
                    try:
                        v = float(p.strip())
                        if v > 0 and (dur is None or v > dur):
                            dur = v
                    except ValueError:
                        continue
            if dur and dur > 0:
                break
            result2 = subprocess.run(
                [cmd, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'csv=p=0', video_path],
                capture_output=True, timeout=10
            )
            if result2.returncode == 0:
                try:
                    dur2 = float(result2.stdout.decode().strip())
                    if dur2 > 0:
                        dur = dur2
                        break
                except ValueError:
                    pass
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            continue

    # 写入缓存
    if dur and dur > 0:
        _update_video_meta_cache(video_path, duration=dur)
    return dur


def get_video_bitrate(video_path):
    """使用 ffprobe 获取视频码率（bps），带内存缓存（LRU 512），失败返回 None"""
    cached = _bitrate_cache.get(video_path)
    if cached is not None:
        # 移动到末尾（最近访问）
        _bitrate_cache.move_to_end(video_path)
        return cached
    result_val = _get_video_bitrate_uncached(video_path)
    # 只缓存有效值，None 不占 LRU 槽位
    if result_val is not None:
        _bitrate_cache[video_path] = result_val
        # 超出上限时淘汰最旧的条目
        if len(_bitrate_cache) > _BITRATE_CACHE_MAX:
            _bitrate_cache.popitem(last=False)
    return result_val


def _get_video_bitrate_uncached(video_path):
    """获取视频码率（无缓存）"""
    ffmpeg_dir = os.path.join(BASE_DIR, 'ffmpeg')
    candidates = [
        os.path.join(ffmpeg_dir, 'ffprobe.exe'),
        'ffprobe',
    ]
    for cmd in candidates:
        if not os.path.exists(cmd) and cmd == candidates[0]:
            continue
        try:
            result = subprocess.run(
                [cmd, '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=bit_rate', '-of', 'csv=p=0',
                 video_path],
                capture_output=True, timeout=15
            )
            if result.returncode == 0:
                val = result.stdout.decode().strip()
                if val and val != 'N/A':
                    fval = float(val)
                    if fval > 0:
                        return fval
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            continue
    try:
        dur = get_video_duration(video_path)
        size = os.path.getsize(video_path)
        if dur and dur > 0:
            return size * 8 / dur
    except (OSError, TypeError):
        pass
    return None


def get_video_info(video_path, force=False):
    """获取视频详细信息：分辨率、文件大小、创建时间（带 mtime 缓存）"""
    # 尝试命中缓存（force=True 时跳过）
    if not force:
        cached = _get_meta_cached(video_path)
        if cached:
            return {
                'width': cached.get('width', 0),
                'height': cached.get('height', 0),
                'file_size': cached.get('file_size', 0),
                'file_size_str': format_file_size(cached.get('file_size', 0)),
                'created_time': cached.get('created_time'),
                'resolution': cached.get('resolution', '未知'),
            }

    info = {
        'width': 0,
        'height': 0,
        'file_size': 0,
        'file_size_str': '0 B',
        'created_time': None,
        'resolution': '未知',
    }
    try:
        ffmpeg_dir = os.path.join(BASE_DIR, 'ffmpeg')
        candidates = [
            os.path.join(ffmpeg_dir, 'ffprobe.exe'),
            'ffprobe',
        ]
        for cmd in candidates:
            if not os.path.exists(cmd) and cmd == candidates[0]:
                continue
            # 使用 ffprobe 获取宽高
            result = subprocess.run(
                [cmd, '-v', 'error', '-select_streams', 'v:0', 
                 '-show_entries', 'stream=width,height', '-of', 'csv=p=0', 
                 video_path],
                capture_output=True, timeout=15
            )
            if result.returncode == 0:
                line = result.stdout.decode().strip()
                if line:
                    parts = line.split(',')
                    if len(parts) >= 2:
                        info['width'] = int(parts[0].strip())
                        info['height'] = int(parts[1].strip())
                        if info['width'] > 0 and info['height'] > 0:
                            info['resolution'] = f"{info['width']}x{info['height']}"
                        break
    except Exception as e:
        logger.info(f"[警告] 使用 ffprobe 获取视频信息失败: {video_path}, 错误: {e}")
    try:
        stat = os.stat(video_path)
        info['file_size'] = stat.st_size
        info['file_size_str'] = format_file_size(stat.st_size)
        try:
            info['created_time'] = time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_ctime))
        except (OSError, ValueError):
            pass
    except OSError:
        pass

    # 写入缓存（只有获取到有效大小时才更新 file_size，避免 file_size=0 被缓存后无法重试）
    fs = int(info['file_size']) if info['file_size'] else 0
    _update_video_meta_cache(video_path,
        width=info['width'], height=info['height'],
        resolution=info['resolution'], file_size=fs if fs > 0 else None,
        created_time=info['created_time'],
        orientation='landscape' if int(info['width']) > int(info['height']) else 'portrait',
    )
    return info


def format_file_size(size):
    """格式化文件大小"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def format_duration(seconds):
    """将秒数格式化为 HH:MM:SS 或 MM:SS"""
    if not seconds:
        return "00:00"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


SUBTITLE_EXTENSIONS = {'.srt', '.ass', '.ssa', '.vtt', '.sup', '.sub'}
SUBTITLE_URL_TEMPLATE = '/subtitle/{series_path}/{episode_index}/{filename}'


def find_subtitles(video_filepath, series_path, episode_index):
    """扫描同名字幕文件，返回字幕列表"""
    video_dir = os.path.dirname(video_filepath)
    video_name_base = os.path.splitext(os.path.basename(video_filepath))[0]
    subtitles = []
    if not os.path.isdir(video_dir):
        return subtitles
    for filename in os.listdir(video_dir):
        name, ext = os.path.splitext(filename)
        if ext.lower() not in SUBTITLE_EXTENSIONS:
            continue
        if name == video_name_base:
            subtitles.append({
                "filename": filename,
                "display_name": ext.upper().lstrip('.').upper() + " 字幕",
                "url": f'/subtitle/{quote(series_path)}/{episode_index}/{quote(filename)}'
            })
    return subtitles


async def stream_transcoded_video(video_path, start_time: float = 0):
    """使用 ffmpeg 实时转码视频，返回异步生成器。start_time 为起始秒数（支持拖动进度）"""
    ffmpeg_cmd = get_ffmpeg_cmd()
    cmd = [
        ffmpeg_cmd,
    ]
    if start_time > 0:
        cmd += ['-ss', str(start_time)]
    cmd += [
        '-fflags', '+genpts',
        '-i', video_path,
    ]
    if is_gpu_transcode_enabled():
        # NVENC 实时转码（恒定比特率，实时流更稳定）
        cmd += [
            '-vcodec', 'h264_nvenc',
            '-acodec', 'aac',
            '-preset', 'p1',          # 实时流用最快预设
            '-cq', '20',
            '-b:v', '0',
            '-rc', 'cbr',
            '-maxrate', '10M',
            '-bufsize', '20M',
            '-pix_fmt', 'yuv420p',
            '-reset_timestamps', '1',
            '-avoid_negative_ts', 'make_zero',
            '-f', 'mp4',
            '-movflags', 'frag_keyframe+empty_moov',
            'pipe:1'
        ]
    else:
        # CPU 软编码
        cmd += [
            '-vcodec', 'libx264',
            '-acodec', 'aac',
            '-preset', 'fast',
            '-crf', '0',
            '-pix_fmt', 'yuv420p',
            '-reset_timestamps', '1',
            '-avoid_negative_ts', 'make_zero',
            '-f', 'mp4',
            '-movflags', 'frag_keyframe+empty_moov',
            'pipe:1'
        ]
    logger.info(f"[FFmpeg] 命令：{' '.join(cmd)}")
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    try:
        assert process.stdout is not None
        while True:
            chunk = await process.stdout.read(1024 * 1024)
            if not chunk:
                break
            yield chunk
    except asyncio.CancelledError:
        process.kill()
        raise
    finally:
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        if process.returncode != 0:
            try:
                assert process.stderr is not None
                stderr_output = await asyncio.wait_for(process.stderr.read(), timeout=3)
                stderr_text = stderr_output.decode('utf-8', errors='replace')[-500:]
                logger.info(f"[FFmpeg 错误] 退出码={process.returncode} | 文件={os.path.basename(video_path)} | {stderr_text}")
            except (asyncio.TimeoutError, Exception):
                logger.info(f"[FFmpeg 错误] 退出码={process.returncode} | 文件={os.path.basename(video_path)}")
def load_config():
    """加载配置文件"""
    default_config = {
        "auto_scan_enabled": True,
        "delete_original_after_transcode": True,
        "auto_transcode": True,
        # 高级设置
        "idle_check_interval": 300,       # 空闲检测间隔（秒）
        "watcher_interval": 5,            # 文件监控轮询间隔（秒）
        "cache_ttl": 300,                 # 系列缓存有效期（秒）
        "meta_workers": 4,                # 后台元数据采集线程数
        "detail_workers": 8,              # 详情页并行查询线程数
        "meta_cache_max": 50000,           # 元数据缓存上限
        "gpu_transcode": True,            # GPU(NVENC) 硬件转码开关
        # 服务器设置
        "server_port": 5000,              # 服务器端口
        "server_host": "0.0.0.0",         # 服务器绑定地址
    }
    if os.path.exists(CONFIG_FILE):
        try:
            config = pb_utils.read_config(CONFIG_FILE)
            if config:
                for k, v in default_config.items():
                    config.setdefault(k, v)
                return config
        except Exception as e:
            logger.info(f"[错误] 读取配置文件失败: {e}")
    return default_config

def save_config(config):
    """保存配置文件"""
    try:
        pb_utils.write_config(CONFIG_FILE, config)
    except OSError as e:
        logger.info(f"[错误] 保存配置文件失败: {e}")
# 视频元数据缓存（分辨率、时长、文件大小、方向），基于文件 mtime 失效
_video_meta_cache: dict = {}
_video_meta_cache_lock = threading.Lock()
_video_meta_cache_file = os.path.join(BASE_DIR, "video_meta_cache.pb")
_VIDEO_META_CACHE_MAX = 50000  # 最大缓存条目数，启动后从 _config 更新


def _load_video_meta_cache():
    """从 video_meta_cache.pb 加载缓存"""
    global _video_meta_cache
    try:
        if os.path.exists(_video_meta_cache_file):
            _video_meta_cache = pb_utils.read_video_meta_cache(_video_meta_cache_file)
            logger.info(f"[缓存] 视频元数据缓存已加载，共 {len(_video_meta_cache)} 条")
    except Exception as e:
        _video_meta_cache = {}
        logger.info(f"[警告] 加载视频元数据缓存失败: {e}")
def _save_video_meta_cache():
    """保存视频元数据缓存到文件"""
    try:
        pb_utils.write_video_meta_cache(_video_meta_cache_file, _video_meta_cache)
    except Exception as e:
        logger.info(f"[警告] 保存视频元数据缓存失败: {e}")
# ====================== 元数据补全（统一实现） ======================

_meta_populate_lock = threading.Lock()
_meta_populate_progress = {"total": 0, "done": 0, "running": False, "current": "", "error": None}


def _is_meta_populate_running():
    """检查元数据补全是否正在运行（原子读取）"""
    with _meta_populate_lock:
        return _meta_populate_progress["running"]


def _start_meta_populate():
    """尝试启动标记，返回 True 表示成功（False 表示已在运行）"""
    with _meta_populate_lock:
        if _meta_populate_progress["running"]:
            return False
        _meta_populate_progress.update({"total": 0, "done": 0, "running": True, "current": "", "error": None})
        return True


def _finish_meta_populate(success=True):
    """结束标记 + 保存缓存"""
    global _meta_populate_progress
    with _meta_populate_lock:
        _meta_populate_progress["running"] = False
    if success:
        _save_video_meta_cache()


def _collect_uncached_videos(series_list):
    """收集需要补全元数据的视频列表（带去重）"""
    uncached = []
    seen = set()
    for series in series_list:
        for v in (series.get('videos') or []):
            fp = v.get('filepath')
            if not fp:
                continue
            if fp in seen:
                continue
            seen.add(fp)
            cached = _get_meta_cached(fp)
            if not cached or cached.get('file_size', 0) == 0:
                uncached.append(fp)
    return uncached


def _fetch_video_meta(fp):
    """采集单个视频的完整元数据（分辨率+时长+文件大小）"""
    cached = _get_meta_cached(fp)
    if cached and cached.get('file_size', 0) > 0:
        return "skip"
    get_video_info(fp, force=True)
    get_video_duration(fp)
    return "ok"


def populate_all_video_meta(series_list=None):
    """元数据补全主函数（并行执行）。

    统一入口：
    - 后台自动调用：由 _run_meta_populate() / 空闲检测子任务触发
    - 手动 API 调用：由 api_populate_meta() 触发
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    global _meta_populate_progress

    if series_list is None:
        series_list = get_all_series()

    try:
        uncached = _collect_uncached_videos(series_list)
        total = len(uncached)

        if total == 0:
            logger.info("[元数据补全] 所有视频已有有效缓存，无需补全")
            _meta_populate_progress["current"] = ""
            _finish_meta_populate(success=False)
            return

        logger.info(f"[元数据补全] 启动，共 {total} 个视频待补全")
        workers = get_config_value("meta_workers", 4)
        done = 0

        def _on_complete(_fp):
            nonlocal done
            done += 1
            with _meta_populate_lock:
                _meta_populate_progress["done"] = int(done)
                _meta_populate_progress["current"] = os.path.basename(_fp)
            if done % 50 == 0 or done == total:
                logger.info(f"[元数据补全] 进度 {done}/{total}")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_video_meta, fp): fp for fp in uncached}
            for future in as_completed(futures):
                fp = futures[future]
                try:
                    future.result()
                except Exception:
                    pass
                _on_complete(fp)

        _meta_populate_progress["current"] = ""
        _finish_meta_populate(success=True)
        logger.info(f"[元数据补全] 完成，共处理 {total} 个视频")
        
        # 元数据补全完成后，检查并生成缺失的缩略图
        try:
            missing_thumbs = _check_and_fix_missing_thumbnails(series_list)
            if missing_thumbs > 0:
                logger.info(f"[缩略图] 补全了 {missing_thumbs} 个缺失缩略图")
        except Exception as e:
            logger.info(f"[缩略图] 检查异常: {e}")
    except Exception as e:
        logger.info(f"[元数据补全] 异常: {e}")
        logger.exception("")
        with _meta_populate_lock:
            _meta_populate_progress["error"] = str(e)
        _finish_meta_populate(success=False)


def _check_and_fix_missing_thumbnails(series_list):
    """检查并生成缺失的缩略图，返回修复数量"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    fixed = 0
    workers = get_config_value("meta_workers", 4)
    
    # 收集需要检查的视频
    videos_to_check = []
    for series in series_list:
        for video in series.get('videos', []):
            fp = video.get('filepath')
            if fp and os.path.isfile(fp):
                videos_to_check.append(fp)
    
    if not videos_to_check:
        return 0
    
    logger.info(f"[缩略图] 检查 {len(videos_to_check)} 个视频的缩略图")
    
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for fp in videos_to_check:
            # 检查缩略图是否存在
            video_dir = os.path.dirname(fp)
            video_name = os.path.splitext(os.path.basename(fp))[0]
            thumb_path = os.path.join(video_dir, video_name + '_thumb.jpg')
            
            if not os.path.exists(thumb_path):
                # 缩略图不存在，生成
                futures[pool.submit(generate_thumbnail, fp, verbose=False)] = fp
        
        for future in as_completed(futures):
            fp = futures[future]
            try:
                result = future.result()
                if result == 'created':
                    fixed += 1
            except Exception:
                pass
    
    return fixed


def _update_video_meta_cache(video_path, **fields):
    """更新指定视频的元数据缓存"""
    try:
        mtime = int(os.path.getmtime(video_path))
    except OSError:
        return
    with _video_meta_cache_lock:
        entry = _video_meta_cache.get(video_path, {})
        entry['_mtime'] = mtime
        # file_size=None 时不更新该字段，保留原值
        if 'file_size' in fields and fields['file_size'] is None:
            fields = dict(fields)
            del fields['file_size']
        entry.update(fields)
        _video_meta_cache[video_path] = entry
        # 超出上限时淘汰旧条目
        if len(_video_meta_cache) > _VIDEO_META_CACHE_MAX:
            keys = list(_video_meta_cache.keys())[:len(_video_meta_cache) - _VIDEO_META_CACHE_MAX]
            for k in keys:
                del _video_meta_cache[k]


def _get_meta_cached(video_path):
    """获取视频的缓存元数据，mtime 不匹配返回 None"""
    try:
        mtime = int(os.path.getmtime(video_path))
    except OSError:
        return None
    with _video_meta_cache_lock:
        entry = _video_meta_cache.get(video_path)
        if entry and abs(entry.get('_mtime', 0) - mtime) < 1:
            return entry
    return None


_config = load_config()
VIDEO_BASE_DIR: str = BASE_DIR
_auto_scan_enabled = bool(_config.get("auto_scan_enabled", True))
_server_port = int(_config.get("server_port", 5000))
_server_host = str(_config.get("server_host", "0.0.0.0")).strip() or "0.0.0.0"
_load_video_meta_cache()

# 从配置更新全局性能参数
SERIES_CACHE_TTL = int(_config.get("cache_ttl", 300))
_VIDEO_META_CACHE_MAX = int(_config.get("meta_cache_max", 50000))


def get_config_value(key: str, default: int) -> int:
    """从 _config 读取配置值，用于后台线程等动态读取场景"""
    return int(_config.get(key, default))

def is_auto_scan_enabled():
    """获取自动扫描开关状态"""
    return _auto_scan_enabled

def set_auto_scan_enabled(enabled):
    """设置自动扫描开关状态"""
    global _auto_scan_enabled
    _auto_scan_enabled = enabled
    _config["auto_scan_enabled"] = enabled
    save_config(_config)

# 缩略图缓存数据结构：{ video_file_path: { "thumb_path": "...", "orientation": "...", "mtime": ... } }
_thumbnail_cache = {}

# 隐藏列表（软删除的合集路径），防止重启后恢复
_hidden_series = set()
_hidden_series_lock = threading.Lock()
_hidden_series_file = os.path.join(BASE_DIR, "hidden_series.pb")

# 扫描时发现的可恢复项（隐藏但仍然存在的目录）
_recoverable_items: list[dict] = []

def load_hidden_series():
    global _hidden_series
    try:
        if os.path.exists(_hidden_series_file):
            _hidden_series = set(pb_utils.read_hidden_series(_hidden_series_file))
    except Exception:
        _hidden_series = set()

def save_hidden_series():
    with _hidden_series_lock:
        try:
            pb_utils.write_hidden_series(_hidden_series_file, list(_hidden_series))
        except Exception as e:
            logger.info(f"[错误] 保存隐藏列表失败: {e}")
def _is_hidden(path):
    """线程安全地检查路径是否在隐藏列表中"""
    with _hidden_series_lock:
        return path in _hidden_series

load_hidden_series()

# B站配色字典（注册为全局变量，避免模板缓存冲突）
_bili_colors = {
    "brand_pink": "#FB7299",
    "brand_pink_light": "#FF4F7D",
    "bg_deep": "#18191C",
    "bg_normal": "#232427",
    "bg_card": "#2D2E32",
    "divider": "#3C3C3E",
    "text_primary": "#FFFFFF",
    "text_secondary": "#9499A0",
    "success": "#00B18E",
    "warning": "#FFB027",
    "error": "#F45A9D",
}

# ====================== 初始化FastAPI应用 ======================
app = FastAPI(title="我的影院")

@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def _chrome_devtools_probe():
    """消除 Chrome DevTools 产生的 404 日志"""
    return Response(status_code=204)

# 使用原生 Jinja2 Environment（避免 Starlette Jinja2Templates 的缓存bug）
_jinja_env = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=True,
)

# 注册全局变量到模板引擎
_jinja_env.globals["bili_colors"] = _bili_colors  # type: ignore[index, reportArgumentType]


def render_template(template_name: str, **context):
    """渲染Jinja2模板并返回HTMLResponse"""
    template = _jinja_env.get_template(template_name)
    html = template.render(**context)
    return HTMLResponse(content=html)


# 挂载静态文件
static_path = os.path.join(BASE_DIR, "static")
if os.path.isdir(static_path):
    app.mount("/static", StaticFiles(directory=static_path), name="static")


# ====================== 工具函数 ======================

def scan_dir_for_series(dir_path, cat_info=None, force_series=False, _in_collection=False, _parent_is_mixed=False):
    """扫描单个目录，返回剧集信息列表。

    目录识别规则：
    - 跳过(Skip)：无视频文件或空目录
    - 合集(Series)：无直连视频，但有1+个子目录包含视频（纯容器目录）
    - 扁平(Flat)：无子目录（或子目录不含视频），有1+个直连视频文件（每个视频独立卡片）
    - 混合(Mixed)：既有直连视频又有含视频的子目录

    Args:
        _in_collection: 内部参数，当 True 时表示该目录处于合集容器的递归扫描上下文中，
                        日志会显示 [合集->扁平/混合] 前缀
        _parent_is_mixed: 内部参数，当 True 时表示父目录是混合目录，扁平子目录应显示
                         [混合->扁平] 前缀
    """
    _ = force_series  # 参数保留以兼容调用方，当前逻辑不再使用
    if not os.path.isdir(dir_path):
        return []

    dir_name = os.path.basename(dir_path)
    is_absolute = os.path.isabs(dir_path)
    logger.info(f"  [扫描] {dir_path}")
    def _build_video_entry(f, fpath):
        """构造视频条目（含转码检测）"""
        ext = os.path.splitext(f)[1].lower()
        entry = {"filename": f, "filepath": fpath}
        if ext in TRANSCODE_FORMATS:
            mp4_path = os.path.splitext(fpath)[0] + '.mp4'
            if not os.path.exists(mp4_path):
                entry["needs_transcode"] = True
                _mark_video_pending_transcode(fpath, dir_path)
        elif ext == '.mp4' and not _is_mp4_browser_playable(fpath):
            entry["needs_transcode"] = True
            _mark_video_pending_transcode(fpath, dir_path)
        return entry

    videos = []               # 直连视频文件列表
    subdirs_have_videos = False  # 是否有子目录含视频
    has_files = False
    try:
        files_in_dir = os.listdir(dir_path)
    except OSError:
        return []
    for f in sorted(files_in_dir, key=_natural_sort_key):
        fpath = os.path.join(dir_path, f)
        if os.path.isfile(fpath) and not f.startswith('.'):
            has_files = True
            ext = os.path.splitext(f)[1].lower()
            if ext in ALLOWED_EXTENSIONS:
                videos.append(_build_video_entry(f, fpath))
        elif os.path.isdir(fpath) and not f.startswith('.'):
            # 检查子目录是否含视频（仅检测一层，不做深度扫描）
            if not subdirs_have_videos:
                try:
                    for sub_f in os.listdir(fpath):
                        if os.path.splitext(sub_f)[1].lower() in ALLOWED_EXTENSIONS:
                            subdirs_have_videos = True
                            break
                except OSError:
                    pass

    # === 目录类型判断 ===
    has_videos = len(videos) > 0
    has_video_subdirs = subdirs_have_videos

    # 跳过：无任何视频（直连+子目录都没有）
    if not has_videos and not has_video_subdirs:
        logger.info(f"  [跳过] {dir_name} （无视频文件{'，有其他文件' if has_files else '，空目录'}）")
        return []

    series_id = dir_path if is_absolute else dir_name

    # --- 辅助函数 ---
    def _make_cover(v):
        """合集封面优先级：cover.jpg > folder.png > 第一个视频缩略图"""
        for cover_ext in ['cover.jpg', 'cover.jpeg', 'cover.png']:
            cover_full = os.path.join(dir_path, cover_ext)
            if os.path.isfile(cover_full):
                prefix = '/cover/abs/' if is_absolute else '/cover/'
                return f'{prefix}{quote(dir_path)}/{quote(cover_ext)}' if is_absolute else f'{prefix}{quote(dir_name)}/{quote(cover_ext)}'
        for cover_ext in ['folder.jpg', 'folder.jpeg', 'folder.png']:
            cover_full = os.path.join(dir_path, cover_ext)
            if os.path.isfile(cover_full):
                prefix = '/cover/abs/' if is_absolute else '/cover/'
                return f'{prefix}{quote(dir_path)}/{quote(cover_ext)}' if is_absolute else f'{prefix}{quote(dir_name)}/{quote(cover_ext)}'
        # 用第一个视频缩略图作为封面
        if v:
            name_without_ext = os.path.splitext(v['filename'])[0]
            thumb_name = name_without_ext + '_thumb.jpg'
            prefix = '/cover/abs/' if is_absolute else '/cover/'
            return f'{prefix}{quote(dir_path)}/{quote(thumb_name)}' if is_absolute else f'{prefix}{quote(dir_name)}/{quote(thumb_name)}'
        return None

    def _make_thumb_url(video):
        """生成视频缩略图URL（用于扁平目录）"""
        if not video:
            return None
        name_without_ext = os.path.splitext(video['filename'])[0]
        thumb_name = name_without_ext + '_thumb.jpg'
        prefix = '/cover/abs/' if is_absolute else '/cover/'
        return f'{prefix}{quote(dir_path)}/{quote(thumb_name)}' if is_absolute else f'{prefix}{quote(dir_name)}/{quote(thumb_name)}'

    def _make_single_entry(name, vid_list, ep_count, unique_path=None, is_flat=False, use_cover=True):
        orientation = 'portrait'
        if vid_list:
            orientation = get_video_orientation(vid_list[0]['filepath'])
        # 为每个视频构造缩略图 URL 和获取分辨率
        for v in vid_list:
            vid_name_ext = os.path.splitext(v['filename'])[0]
            thumb_name = vid_name_ext + '_thumb.jpg'
            if is_absolute:
                v['thumbnail'] = f'/cover/abs/{quote(dir_path)}/{quote(thumb_name)}'
            else:
                v['thumbnail'] = f'/cover/{quote(dir_name)}/{quote(thumb_name)}'
            # 从元数据缓存获取分辨率（竖屏用宽度，横屏用高度）
            with _video_meta_cache_lock:
                cached = _video_meta_cache.get(v['filepath'])
            if cached:
                w = cached.get('width', 0) or 0
                h = cached.get('height', 0) or 0
                # 竖屏用宽度，横屏用高度
                if w > h:
                    size = h  # 横屏用高度
                else:
                    size = w  # 竖屏用宽度
                if size >= 2160:
                    v['resolution'] = '4K'
                elif size >= 1080:
                    v['resolution'] = '1080P'
                elif size >= 720:
                    v['resolution'] = '720P'
                elif size > 0:
                    v['resolution'] = f'{size}p'
                else:
                    v['resolution'] = ''
        # 封面：一级目录（扁平/混合）不使用cover.jpg，只用缩略图；其他情况可选cover.jpg
        cover = None
        if use_cover and vid_list:
            cover = _make_cover(vid_list[0])
        if not cover and is_flat and vid_list:
            cover = _make_thumb_url(vid_list[0])
        return {
            "name": name,
            "path": unique_path or series_id,
            "total_episodes": ep_count,
            "episode_count": len(vid_list),
            "videos": vid_list,
            "cover": cover,
            "orientation": orientation,
            "category": cat_info or get_series_category(dir_name),
            "_is_abs": is_absolute,
            "_abs_path": dir_path if is_absolute else None,
        }

    # === 按新规则分类 ===

    # 判断是否为一級目录或混合下的子目录（这些目录不使用cover）
    is_top_level = not _in_collection and not _parent_is_mixed
    is_mixed_subdir = _parent_is_mixed  # 混合目录下的子目录
    use_cover = not (is_top_level or is_mixed_subdir)

    # 1. 混合目录：既有直连视频，又有含视频的子目录
    if has_videos and has_video_subdirs:
        prefix = "[合集->混合]" if _in_collection else "[混合]"
        logger.info(f"  {prefix} {dir_name} ({len(videos)}个视频 + 有子目录)")
        results = []
        for v in videos:
            name = os.path.splitext(v['filename'])[0]
            unique_id = series_id + '/' + v['filename']
            results.append(_make_single_entry(name, [v], 1, unique_path=unique_id, is_flat=True, use_cover=use_cover))
        return results

    # 2. 合集（容器目录）：无直连视频，但有含视频的子目录
    if not has_videos and has_video_subdirs:
        # 纯容器目录本身不产生 series 条目，由 scan_dir_recursive 递归扫描各子目录生成
        logger.info(f"  [合集容器] {dir_name} （纯子目录，由递归扫描处理）")
        return []

    # 3. 扁平目录：有直连视频，无含视频的子目录
    if has_videos and not has_video_subdirs:
        if _parent_is_mixed:
            prefix = "[混合->扁平]"
        elif _in_collection:
            prefix = "[合集->扁平]"
        else:
            prefix = "[扁平]"
        
        # 一级扁平目录：每个视频单独显示为卡片
        # 合集/混合下的扁平子目录：整个目录作为一个合集
        is_sub_flat = _parent_is_mixed or _in_collection
        
        if is_sub_flat:
            logger.info(f"  {prefix} {dir_name} ({len(videos)}个视频)")
            # 整个目录作为一个合集，包含所有视频
            name = dir_name
            unique_id = series_id
            results = [_make_single_entry(name, videos, len(videos), unique_path=unique_id, is_flat=True, use_cover=use_cover)]
            logger.info(f"  [扁平目录] {dir_name} -> 1 个合集（{len(videos)}个视频）")
        else:
            # 一级扁平目录：每个视频独立卡片
            logger.info(f"  {prefix} {dir_name} ({len(videos)}个视频)")
            results = []
            for v in videos:
                name = os.path.splitext(v['filename'])[0]
                unique_id = series_id + '/' + v['filename']
                results.append(_make_single_entry(name, [v], 1, unique_path=unique_id, is_flat=True, use_cover=use_cover))
            logger.info(f"  [扁平目录] {dir_name} -> {len(results)} 个独立视频")
        return results

    # 不应到达这里
    return []


def scan_dir_recursive(root_path, cat_info=None, seen_paths=None, series_list=None, max_depth=3, force_series=False):
    """递归扫描目录及其子目录，收集所有含视频的目录"""
    if seen_paths is None:
        seen_paths = set()
    if series_list is None:
        series_list = []

    def _walk(current_path, depth, _in_coll=False, _parent_is_mixed=False):
        if depth > max_depth or current_path in seen_paths:
            return
        if _is_hidden(current_path):
            logger.info(f"  [隐藏] 跳过已删除的目录: {current_path}")
            # 记录为可恢复项（目录或文件仍然存在）
            if os.path.exists(current_path):
                _recoverable_items.append({
                    "name": os.path.basename(current_path),
                    "path": current_path,
                })
            return
        seen_paths.add(current_path)

        results = scan_dir_for_series(current_path, cat_info, force_series=force_series, _in_collection=_in_coll, _parent_is_mixed=_parent_is_mixed)
        for result in results:
            series_list.append(result)
        if results:
            update_series_cache_entry(current_path, results)

        def _has_video_subdirs(path):
            """检查目录是否有含视频的子目录"""
            try:
                for item in os.listdir(path):
                    full = os.path.join(path, item)
                    if os.path.isdir(full) and not item.startswith('.'):
                        for sub_f in os.listdir(full):
                            if os.path.splitext(sub_f)[1].lower() in ALLOWED_EXTENSIONS:
                                return True
            except OSError:
                pass
            return False

        def _has_direct_videos(path):
            """检查目录是否有直连视频"""
            try:
                for item in os.listdir(path):
                    if os.path.isfile(os.path.join(path, item)):
                        ext = os.path.splitext(item)[1].lower()
                        if ext in ALLOWED_EXTENSIONS:
                            return True
            except OSError:
                pass
            return False

        def _is_collection_container(path):
            """检查目录是否为合集容器（无直连视频但有含视频的子目录）"""
            return os.path.isdir(path) and not _has_direct_videos(path) and _has_video_subdirs(path)

        is_mixed = os.path.isdir(current_path) and _has_direct_videos(current_path) and _has_video_subdirs(current_path)
        is_collection = _is_collection_container(current_path)
        try:
            for item in sorted(os.listdir(current_path)):
                full = os.path.join(current_path, item)
                if os.path.isdir(full) and not item.startswith('.'):
                    _walk(full, depth + 1, _in_coll=_in_coll or is_mixed or is_collection, _parent_is_mixed=is_mixed)
        except OSError:
            pass

    _walk(root_path, 0)
    return series_list




def get_all_series() -> list[dict]:
    """扫描所有合集目录，返回剧集列表（带 TTL 内存缓存）"""
    global _series_list_cache, _series_list_cache_time, _series_cache
    now = time.time()

    def _filter_hidden(lst):
        """过滤已隐藏的条目"""
        return [s for s in lst if not _is_hidden(s.get('path', ''))]

    with _series_list_cache_lock:
        # TTL 内直接返回内存缓存（仍需过滤隐藏项）
        if _series_list_cache is not None and (now - _series_list_cache_time) < SERIES_CACHE_TTL:
            return _filter_hidden(_series_list_cache)
        # 自动扫描关闭时：优先使用已有内存缓存，否则从缓存文件恢复
        if not is_auto_scan_enabled():
            if _series_list_cache is not None:
                return _filter_hidden(_series_list_cache)
            # 尝试从磁盘缓存文件恢复
            if os.path.exists(SERIES_CACHE_FILE):
                _restore_series_from_cache()
                if _series_list_cache is not None:
                    return _filter_hidden(_series_list_cache)
                else:
                    # 缓存文件存在但恢复失败（可能是空文件），触发一次全量扫描重建缓存
                    logger.info("[警告] 缓存恢复失败，触发全量扫描重建缓存")
                    series_list = _get_all_series_uncached()
                    _series_list_cache = series_list
                    _series_list_cache_time = now
                    return series_list
            # 无缓存时返回空列表（不执行扫描）
            logger.info("[警告] 自动扫描已关闭且缓存为空，返回空列表")
            return []
        # 自动扫描开启时：
        # 1. 如果有缓存，先返回缓存数据
        # 2. 同时在后台执行扫描更新缓存
        if _series_list_cache is not None:
            # 启动后台扫描
            threading.Thread(target=_refresh_series_cache, daemon=True).start()
            return _filter_hidden(_series_list_cache)
        # 无缓存时：
        # 1. 先从缓存文件恢复
        # 2. 启动后台扫描
        _restore_series_from_cache()
        if _series_list_cache is not None:
            threading.Thread(target=_refresh_series_cache, daemon=True).start()
            return _filter_hidden(_series_list_cache)
        # 完全无缓存时：
        # 在锁外执行同步扫描，避免阻塞其他请求
        pass  # 先释放锁，在下面执行扫描
    # --- 锁外区域 ---
    # 完全无缓存时，执行同步扫描（用 _series_scan_lock 防并发重复扫描）
    with _series_scan_lock:
        series_list = _get_all_series_uncached()
        with _series_list_cache_lock:
            if _series_list_cache is not None:
                series_list = _series_list_cache  # 其他线程已填充
            else:
                _series_list_cache = series_list
                _series_list_cache_time = now
        return series_list

def _refresh_series_cache():
    """后台刷新剧集缓存（增量更新，不阻塞用户）"""
    global _series_list_cache, _series_list_cache_time
    try:
        new_series_list = _get_all_series_uncached()
        with _series_list_cache_lock:
            _incremental_merge_series(new_series_list)
            _series_list_cache_time = time.time()
        logger.info("[后台扫描] 完成，缓存已更新（增量）")
    except Exception as e:
        logger.info(f"[后台扫描] 失败: {e}")


def _incremental_merge_series(new_series_list: list[dict]):
    """增量合并新扫描结果到现有缓存
    
    策略：
    1. 按 path 建立新旧映射
    2. 新增：path 不在旧列表中 → 加入
    3. 删除：path 不在新列表中 → 移除
    4. 更新：path 存在但数据变化 → 更新
    5. 不变：path 存在且数据相同 → 保留
    """
    global _series_list_cache
    
    if _series_list_cache is None:
        # 无旧缓存，直接替换
        _series_list_cache = new_series_list
        return
    
    # 按 path 建立映射
    old_map = {s['path']: s for s in _series_list_cache if 'path' in s}
    new_map = {s['path']: s for s in new_series_list if 'path' in s}
    
    added = 0
    removed = 0
    updated = 0
    unchanged = 0
    
    # 新增和更新
    for path, new_entry in new_map.items():
        if path not in old_map:
            # 新增
            added += 1
        else:
            old_entry = old_map[path]
            # 简单判断：如果视频数量或名称变化，视为更新
            old_videos = set(v.get('name', '') for v in old_entry.get('videos', []))
            new_videos = set(v.get('name', '') for v in new_entry.get('videos', []))
            if old_videos != new_videos:
                # 更新
                old_map[path] = new_entry
                updated += 1
            else:
                # 不变
                unchanged += 1
    
    # 删除
    for path in list(old_map.keys()):
        if path not in new_map:
            del old_map[path]
            removed += 1
    
    # 合并结果
    _series_list_cache = list(old_map.values())
    
    logger.info(f"[增量合并] +{added} 新增，-{removed} 删除，~{updated} 更新，= {unchanged} 不变 → 共 {len(_series_list_cache)} 个条目")
def _get_all_series_uncached():
    """实际执行目录扫描的内部函数"""
    global _recoverable_items
    series_list = []
    seen_paths = set()
    cats = load_categories()
    _recoverable_items = []  # 每次扫描前清空

    logger.info(f"\n[扫描开始] 共 {len(cats)} 个分类")
    cat_dir_map = {}
    for cat in cats:
        dirs = cat.get('dirs') or []
        logger.info(f"  分类「{cat.get('name', '?')}」: {len(dirs)} 个目录 -> {dirs}")
        for d in dirs:
            cat_dir_map[str(d)] = cat

    for dir_path, cat in cat_dir_map.items():
        if os.path.isabs(dir_path):
            full_path = dir_path
        else:
            full_path = os.path.join(VIDEO_BASE_DIR, dir_path)

        if full_path in seen_paths:
            continue

        if not os.path.isdir(full_path):
            logger.info(f"  [跳过] 目录不存在: {full_path}")
            continue

        if _is_hidden(full_path):
            logger.info(f"  [隐藏] 跳过已删除的目录: {full_path}")
            # 记录为可恢复项（目录或文件仍然存在）
            if os.path.exists(full_path):
                _recoverable_items.append({
                    "name": os.path.basename(full_path),
                    "path": full_path,
                })
            continue

        cat_info = {**_get_default_category(), "dirs": cat.get('dirs', [])}

        cached = _series_cache.get(full_path)
        if cached and not is_dir_changed(full_path):
            logger.info(f"  [缓存命中] {full_path} (递归)")
            for sp, sd in _series_cache.items():
                if sp.startswith(full_path) and ('data' in sd) and sp not in seen_paths:
                    if _is_hidden(sp):
                        continue
                    data = sd['data']
                    filtered = _filter_pending(data)
                    if filtered is None:
                        continue
                    if isinstance(filtered, list):
                        for item in filtered:
                            item['category'] = cat_info
                            series_list.append(item)
                        seen_paths.add(sp)
                    else:
                        filtered['category'] = cat_info
                        series_list.append(filtered)
                        seen_paths.add(sp)
            continue
        else:
            logger.info(f"  [递归扫描] {full_path}")
            before_count = len(series_list)
            has_subdirs = any(
                os.path.isdir(os.path.join(full_path, d)) and not d.startswith('.')
                for d in os.listdir(full_path)
            ) if os.path.isdir(full_path) else False
            scan_dir_recursive(full_path, cat_info, seen_paths, series_list, force_series=has_subdirs)
            # 对递归扫描新增的结果应用 _filter_pending 过滤
            new_items = series_list[before_count:]
            filtered_new = []
            for item in new_items:
                result = _filter_pending(item)
                if result is None:
                    continue
                if isinstance(result, list):
                    filtered_new.extend(result)
                else:
                    filtered_new.append(result)
            series_list[before_count:] = filtered_new
            new_count = len(filtered_new)
            logger.info(f"  [递归完成] {full_path} -> 找到 {new_count} 个视频目录")
    skip_dirs = {'static', 'templates', '__pycache__', 'uploads', '.git'}
    for item in sorted(os.listdir(VIDEO_BASE_DIR)):
        if item.startswith('.') or item in skip_dirs:
            continue
        full_path = os.path.join(VIDEO_BASE_DIR, item)
        if full_path in seen_paths or not os.path.isdir(full_path):
            continue
        if _is_hidden(full_path):
            logger.info(f"  [隐藏] 跳过已删除的目录: {full_path}")
            # 记录为可恢复项（目录或文件仍然存在）
            if os.path.exists(full_path):
                _recoverable_items.append({
                    "name": os.path.basename(full_path),
                    "path": full_path,
                })
            continue
        seen_paths.add(full_path)

        cached = _series_cache.get(full_path)
        if cached and not is_dir_changed(full_path):
            logger.info(f"  [缓存命中] {full_path}")
            data = cached['data']
            filtered = _filter_pending(data)
            if filtered:
                if isinstance(filtered, list):
                    for item in filtered:
                        item.setdefault('category', {"id": "default", "name": "合集", "icon": "tv", "color": "#fb7299"})
                    series_list.extend(filtered)
                else:
                    filtered.setdefault('category', {"id": "default", "name": "合集", "icon": "tv", "color": "#fb7299"})
                    series_list.append(filtered)
        else:
            results = scan_dir_for_series(full_path, force_series=True)
            if results:
                filtered_results = _filter_pending(results)
                if filtered_results:
                    update_series_cache_entry(full_path, results)
                    for item in filtered_results:
                        item.setdefault('category', {"id": "default", "name": "合集", "icon": "tv", "color": "#fb7299"})
                    series_list.extend(filtered_results)

    save_series_cache()
    # 最终过滤：移除已软删除的条目（特别是扁平目录下的单个视频）
    filtered_list = []
    for s in series_list:
        if _is_hidden(s.get('path', '')):
            # 收集到可恢复项
            _recoverable_items.append({
                "name": s.get('name', os.path.basename(s.get('path', ''))),
                "path": s.get('path', ''),
            })
        else:
            filtered_list.append(s)
    series_list = filtered_list
    logger.info(f"[扫描完成] 共找到 {len(series_list)} 个含视频的目录")
    # 自动扫描开启时，立即补全缺失的缩略图和封面
    if is_auto_scan_enabled():
        try:
            stats = _generate_missing_assets(series_list, verbose=False)
            if stats['thumbnails'] > 0 or stats['covers'] > 0:
                save_thumbnail_cache()
                logger.info(f"[扫描补全] {stats['thumbnails']} 个缩略图，{stats['covers']} 个封面")
        except Exception as e:
            logger.info(f"[扫描补全] 异常: {e}")
    _save_video_meta_cache()
    # 后台补全所有视频的元数据缓存（不阻塞扫描返回，防重入）
    if _start_meta_populate():
        threading.Thread(target=populate_all_video_meta, args=(series_list,), daemon=True).start()
    return series_list


def get_available_dirs():
    """获取所有含视频文件的目录名列表（用于设置页下拉选择）"""
    skip = {'static', 'templates', '__pycache__', 'uploads', '.git'}
    dirs = []
    for item in sorted(os.listdir(VIDEO_BASE_DIR)):
        full_path = os.path.join(VIDEO_BASE_DIR, item)
        if not os.path.isdir(full_path) or item.startswith('.') or item in skip:
            continue
        try:
            for f in os.listdir(full_path):
                fpath = os.path.join(full_path, f)
                if os.path.isfile(fpath) and not f.startswith('.'):
                    ext = os.path.splitext(f)[1].lower()
                    if ext in ALLOWED_EXTENSIONS:
                        dirs.append(item)
                        break
        except PermissionError:
            continue
    return dirs


def load_thumbnail_cache():
    """加载缩略图缓存数据库"""
    global _thumbnail_cache
    if os.path.exists(THUMBNAIL_CACHE_FILE):
        try:
            _thumbnail_cache = pb_utils.read_thumbnail_cache(THUMBNAIL_CACHE_FILE)
            logger.info(f"[缩略图缓存] 已加载 {len(_thumbnail_cache)} 条缓存记录")
        except Exception as e:
            logger.info(f"[缩略图缓存] 加载失败: {e}")
            _thumbnail_cache = {}
    else:
        logger.info("[缩略图缓存] 未找到缓存文件，将创建新缓存")
        _thumbnail_cache = {}


def save_thumbnail_cache():
    """保存缩略图缓存到数据库"""
    try:
        pb_utils.write_thumbnail_cache(THUMBNAIL_CACHE_FILE, _thumbnail_cache)
        logger.info(f"[缩略图缓存] 已保存 {len(_thumbnail_cache)} 条缓存记录")
    except Exception as e:
        logger.info(f"[缩略图缓存] 保存失败: {e}")
_series_cache = {}
_series_cache_lock = threading.Lock()
_series_list_cache = None
_series_list_cache_time = 0


def _find_series_in_cache(series_path: str):
    """从内存缓存中查找指定系列，不触发全量扫描。返回 series dict 或 None。"""
    assert series_path is not None, "series_path cannot be None"
    
    if _series_list_cache is not None:
        for s in _series_list_cache:
            s_path = s.get('path')
            # 直接比较
            if s_path == series_path:
                return s
            # 尝试标准化路径比较
            try:
                if os.path.normpath(s_path) == os.path.normpath(series_path):
                    return s
                # 处理扁平目录下的单个视频路径（Windows路径分隔符）
                if '\\' in series_path and os.path.isdir(s_path):
                    parent_dir = series_path.split('\\')[0]
                    if os.path.normpath(s_path) == os.path.normpath(parent_dir):
                        return s
                # 处理URL编码的路径
                if '/' in series_path and os.path.isdir(s_path):
                    parent_dir = series_path.split('/')[0]
                    if os.path.normpath(s_path) == os.path.normpath(parent_dir):
                        return s
            except Exception as e:
                logger.info(f"[警告] 路径标准化比较失败: {e}")
    return None


def _require_series(series_path: str):
    """获取指定系列（优先缓存，缓存未命中才调 get_all_series）"""
    assert series_path is not None, "series_path cannot be None"
    
    series = _find_series_in_cache(series_path)
    if series:
        return series
    all_series = get_all_series()
    return next((s for s in all_series if s.get('path') == series_path), None)

def load_series_cache():
    """加载视频系列缓存数据库"""
    global _series_cache, _series_list_cache, _series_list_cache_time
    if os.path.exists(SERIES_CACHE_FILE):
        try:
            _series_cache = pb_utils.read_series_cache(SERIES_CACHE_FILE)
            logger.info(f"[系列缓存] 已加载 {len(_series_cache)} 条记录")
            _restore_series_from_cache()
        except Exception as e:
            logger.info(f"[系列缓存] 加载失败: {e}")
            _series_cache = {}
    else:
        _series_cache = {}
        logger.info("[系列缓存] 未找到缓存文件")
def _restore_series_from_cache():
    """从缓存恢复剧集列表（自动扫描关闭时使用）"""
    global _series_list_cache, _series_list_cache_time
    if _series_list_cache is not None:
        return
    if not os.path.exists(SERIES_CACHE_FILE):
        logger.info(f"[系列缓存] 缓存文件不存在: {SERIES_CACHE_FILE}")
        return
    try:
        cache_data = pb_utils.read_series_cache(SERIES_CACHE_FILE)
    except Exception as e:
        logger.info(f"[系列缓存] 读取失败: {e}")
        return
    
    cats = load_categories()
    cat_map = {}
    for cat in cats:
        for d in (cat.get('dirs') or []):
            d_str = str(d).replace('/', '\\')
            cat_map[d_str] = cat
    cat_dirs = sorted(cat_map.keys(), key=len, reverse=True)
    if not cat_dirs:
        logger.info("[系列缓存] 警告：无分类目录配置，无法恢复缓存")
        return
    
    logger.info(f"[系列缓存] 恢复数据，分类目录: {cat_dirs}")
    series_list = []
    for dir_path, cd in cache_data.items():
        data = cd.get('data')
        if not data:
            continue
        # 跳过已软删除的系列
        if _is_hidden(dir_path):
            continue
        cat = None
        for cat_dir in cat_dirs:
            if dir_path.startswith(cat_dir):
                cat = cat_map[cat_dir]
                break
        if cat is None:
            continue
        if isinstance(data, list):
            for item in data:
                # 也检查条目自身的 path，防止路径格式不一致导致漏网
                if _is_hidden(item.get('path', '')):
                    continue
                # 确保每个条目都有正确的路径
                if not item.get('path'):
                    item['path'] = dir_path
                item['category'] = cat
                series_list.append(item)
        else:
            if _is_hidden(data.get('path', '')):
                continue
            # 确保单个条目也有正确的路径
            if not data.get('path'):
                data['path'] = dir_path
            data['category'] = cat
            series_list.append(data)
    
    _series_list_cache = series_list
    _series_list_cache_time = time.time()
    logger.info(f"[系列缓存] 已恢复 {len(series_list)} 个条目")
def save_series_cache():
    """保存系列缓存到数据库（原子写入）"""
    try:
        pb_utils.write_series_cache(SERIES_CACHE_FILE, _series_cache)
        logger.info(f"[系列缓存] 已保存 {len(_series_cache)} 条记录")
    except Exception as e:
        logger.info(f"[系列缓存] 保存失败: {e}")
def _clear_series_cache():
    """清除 series_cache.pb 强制重新扫描"""
    global _series_cache
    try:
        if os.path.exists(SERIES_CACHE_FILE):
            os.remove(SERIES_CACHE_FILE)
            logger.info("[缓存清除] series_cache.pb 已删除")
    except Exception as e:
        logger.info(f"[缓存清除] 删除 series_cache.pb 失败: {e}")
    _series_cache = {}
    with _series_list_cache_lock:
        _series_list_cache = None
        _series_list_cache_time = 0
    _series_list_cache_time = 0

def get_dir_mtime(dir_path):
    """获取目录的修改时间（用于判断是否需要重新扫描）"""
    try:
        return os.path.getmtime(dir_path)
    except OSError:
        return 0

def is_dir_changed(dir_path):
    """检查目录是否有变化（对比缓存中的 mtime）"""
    current_mtime = get_dir_mtime(dir_path)
    cached = _series_cache.get(dir_path)
    if not cached or cached.get('mtime') != current_mtime:
        return True
    return False

def update_series_cache_entry(dir_path, series_data):
    """更新单条系列缓存记录"""
    _series_cache[dir_path] = {
        "mtime": get_dir_mtime(dir_path),
        "data": series_data
    }

def cleanup_stale_cache():
    """清理已删除目录/视频的各类缓存记录"""
    removed = 0
    to_remove = []
    for dir_path in list(_series_cache.keys()):
        if not os.path.exists(dir_path):
            to_remove.append(dir_path)
    for dir_path in to_remove:
        del _series_cache[dir_path]
        logger.info(f"  [清理] 已删除目录: {dir_path}")
        removed += 1

    thumb_removed = 0
    for video_path in list(_thumbnail_cache.keys()):
        if not os.path.exists(video_path):
            del _thumbnail_cache[video_path]
            thumb_removed += 1

    # 清理不存在的视频的元数据缓存
    meta_removed = 0
    with _video_meta_cache_lock:
        meta_to_remove = [k for k in _video_meta_cache if not os.path.exists(k)]
        for k in meta_to_remove:
            del _video_meta_cache[k]
            meta_removed += 1

    # 清理不存在的视频的码率缓存
    bitrate_removed = 0
    for k in list(_bitrate_cache.keys()):
        if not os.path.exists(k):
            del _bitrate_cache[k]
            bitrate_removed += 1

    if removed or thumb_removed:
        save_series_cache()
        save_thumbnail_cache()
    if meta_removed:
        _save_video_meta_cache()
    parts = []
    if removed:
        parts.append(f"{removed} 个已删除系列")
    if thumb_removed:
        parts.append(f"{thumb_removed} 条失效缩略图")
    if meta_removed:
        parts.append(f"{meta_removed} 条失效元数据")
    if bitrate_removed:
        parts.append(f"{bitrate_removed} 条失效码率")
    if parts:
        logger.info(f"[清理完成] 移除 {', '.join(parts)}")
    else:
        logger.info("[清理完成] 无需清理")
@app.post("/api/video/delete")
async def api_delete_video(request: Request):
    """从数据库中删除视频或视频目录（软删除，不删除实际文件）"""
    try:
        body = await request.json()
        dir_path = unquote(body.get('path', ''))
        is_series = body.get('is_series', True)

        if os.path.isabs(dir_path):
            full_path = dir_path
            if not _is_abs_path_allowed(full_path):
                return JSONResponse({"ok": False, "error": "禁止操作"})
        else:
            full_path = os.path.join(VIDEO_BASE_DIR, dir_path)
            if not _is_path_safe(VIDEO_BASE_DIR, full_path):
                return JSONResponse({"ok": False, "error": "禁止操作"})

        with _hidden_series_lock:
            _hidden_series.add(full_path)
            if is_series:
                for k in list(_series_cache.keys()):
                    if k.startswith(full_path + os.sep) or k.startswith(full_path + '/'):
                        _hidden_series.add(k)
        save_hidden_series()

        cache_keys_to_remove = [full_path] if full_path in _series_cache else []
        if is_series:
            for k in list(_series_cache.keys()):
                if k.startswith(full_path + os.sep) or k.startswith(full_path + '/'):
                    if k not in cache_keys_to_remove:
                        cache_keys_to_remove.append(k)

        for k in cache_keys_to_remove:
            if k in _series_cache:
                del _series_cache[k]
                logger.info(f"[软删除-系列] {k}")
        to_remove_thumb = []
        for k in _thumbnail_cache:
            if any(k.startswith(pk) for pk in cache_keys_to_remove):
                to_remove_thumb.append(k)
        for k in to_remove_thumb:
            del _thumbnail_cache[k]
            logger.info(f"[软删除-缩略图] {k}")
        save_series_cache()
        save_thumbnail_cache()

        # 清除列表缓存，确保刷新页面后不显示已删除项
        global _series_list_cache, _series_list_cache_time
        _series_list_cache = None
        _series_list_cache_time = 0

        return JSONResponse({"ok": True, "removed": len(cache_keys_to_remove)})
    except Exception as e:
        logger.info(f"[删除失败] {e}")
        logger.exception("")
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/recoverable")
async def api_get_recoverable():
    """获取可恢复项：之前删除但磁盘上仍然存在的目录或视频文件"""
    items = []
    with _hidden_series_lock:
        hidden_snapshot = set(_hidden_series)
    for path in hidden_snapshot:
        if os.path.exists(path):
            items.append({"name": os.path.basename(path), "path": path})
    return JSONResponse({"items": items})




@app.post("/api/video/restore")
async def api_restore_video(request: Request):
    """恢复之前软删除的系列/视频"""
    try:
        body = await request.json()
        paths = body.get('paths', [])
        if not isinstance(paths, list):
            return JSONResponse({"ok": False, "error": "paths 应为数组"})

        restored = 0
        with _hidden_series_lock:
            for p in paths:
                if p in _hidden_series:
                    _hidden_series.discard(p)
                    restored += 1
                    # 同时移除子路径
                    for k in list(_hidden_series):
                        if k.startswith(p + os.sep) or k.startswith(p + '/'):
                            _hidden_series.discard(k)
                            restored += 1
        save_hidden_series()

        # 清除列表缓存，下次刷新时重新扫描
        global _series_list_cache, _series_list_cache_time, _recoverable_items
        _series_list_cache = None
        _series_list_cache_time = 0
        _recoverable_items = []  # 清空可恢复列表

        logger.info(f"[恢复] 已恢复 {restored} 个路径")
        return JSONResponse({"ok": True, "restored": restored})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


def generate_thumbnail(video_path, verbose=True):
    """从视频文件中提取缩略图，保存到视频所在目录。
    已存在则跳过。返回：'cached'(已有)/'created'(新建)/None(失败)
    使用缩略图缓存数据库避免重复检测。
    """
    video_dir = os.path.dirname(video_path)
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    thumb_name = video_name + '_thumb.jpg'
    output_path = os.path.join(video_dir, thumb_name)

    # 检查缓存
    video_mtime = os.path.getmtime(video_path)
    cached = _thumbnail_cache.get(video_path)
    if cached and os.path.exists(cached['thumb_path']):
        if cached.get('mtime') == video_mtime:
            if verbose:
                logger.info(f"  [缓存命中] {os.path.basename(video_path)}")
            return 'cached'

    # 已存在但需要验证尺寸是否正确
    if os.path.exists(output_path):
        if verify_thumbnail_size(video_path):
            _thumbnail_cache[video_path] = {
                'thumb_path': output_path,
                'mtime': video_mtime
            }
            if verbose:
                logger.info(f"  [缓存验证] {os.path.basename(video_path)}")
            return 'cached'
        else:
            if verbose:
                logger.info(f"  [缩略图无效] 删除并重新生成: {os.path.basename(video_path)}")
            try:
                os.remove(output_path)
            except OSError:
                pass

    cap = cv2.VideoCapture(video_path)
    try:
        if not cap.isOpened():
            if verbose:
                logger.info(f"[警告] 无法打开视频文件: {video_path}")
            return None

        # 获取视频分辨率以确定缩略图方向
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # 跳转到第3秒位置取帧（避免黑屏）
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps and fps > 0 and fps == fps:  # fps == fps 过滤 NaN
            target_frame = min(int(fps * 3), 100)
        else:
            target_frame = 30
        cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)

        ret, frame = cap.read()

        # 如果第一次读取失败，重置状态后重试
        if not ret or frame is None:  # pyright: ignore[reportUnnecessaryComparison]
            cap.release()
            cap = cv2.VideoCapture(video_path)
            if cap.isOpened():
                ret, frame = cap.read()

        if ret and frame is not None:  # pyright: ignore[reportUnnecessaryComparison]
            # 根据视频方向选择缩略图尺寸
            is_landscape = w > h
            if is_landscape:
                # 横向视频：320x180 (16:9)
                target_w, target_h = 320, 180
            else:
                # 纵向视频：180x320 (9:16)
                target_w, target_h = 180, 320

            fh, fw = frame.shape[:2]
            if fw / fh > target_w / target_h:
                new_w = target_w
                new_h = max(int(fh * target_w / fw), target_h)
            else:
                new_h = target_h
                new_w = max(int(fw * target_h / fh), target_w)

            resized = cv2.resize(frame, (new_w, new_h))

            top = (new_h - target_h) // 2
            left = (new_w - target_w) // 2
            cropped = resized[top:top+target_h, left:left+target_w]

            cv2.imencode('.jpg', cropped, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tofile(output_path)
            _thumbnail_cache[video_path] = {
                'thumb_path': output_path,
                'mtime': video_mtime
            }
            orient_tag = "横向" if is_landscape else "竖屏"
            if verbose:
                logger.info(f"  [生成完成] {os.path.basename(video_path)} -> {thumb_name} ({orient_tag})")
            return 'created'
        if verbose:
            logger.info(f"[警告] 无法读取帧: {video_path}")
        return None
    except Exception as e:
        if verbose:
            logger.info(f"[错误] 生成缩略图失败: {video_path}, 错误: {e}")
        return None
    finally:
        cap.release()


def verify_thumbnail_size(video_path):
    """验证缩略图尺寸是否正确，不正确返回False"""
    video_dir = os.path.dirname(video_path)
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    thumb_name = video_name + '_thumb.jpg'
    thumb_path = os.path.join(video_dir, thumb_name)

    if not os.path.exists(thumb_path):
        return False

    try:
        img = cv2.imdecode(np.fromfile(thumb_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return False
        h, w = img.shape[:2]
        is_landscape = w > h
        if is_landscape:
            expected_w, expected_h = 320, 180
        else:
            expected_w, expected_h = 180, 320
        if w != expected_w or h != expected_h:
            os.remove(thumb_path)
            return False
        return True
    except (OSError, cv2.error):
        return False


def verify_and_regenerate_thumbnails(all_series, verbose=True):
    """检测所有缩略图尺寸，不正确的重新生成（并行验证 + 生成）"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    fixed = 0
    workers = get_config_value("meta_workers", 4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for series in all_series:
            for video in series['videos']:
                fp = video.get('filepath')
                if fp:
                    futures[pool.submit(verify_thumbnail_size, fp)] = (fp, video['filepath'])

        for future in as_completed(futures):
            fp, original_fp = futures[future]
            try:
                ok = future.result()
            except Exception:
                ok = False
            if not ok:
                result = generate_thumbnail(original_fp, verbose=verbose)
                if result == 'created':
                    fixed += 1
    return fixed


def generate_series_cover(series, _verbose=True):
    """为合集生成封面图片（cover.jpg），从第一个视频的缩略图复制"""
    if not series or not series.get('videos'):
        return False

    video_dir = os.path.dirname(series['videos'][0]['filepath'])
    cover_path = os.path.join(video_dir, 'cover.jpg')
    first_video = series['videos'][0]
    thumb_name = os.path.splitext(first_video['filename'])[0] + '_thumb.jpg'
    thumb_path = os.path.join(video_dir, thumb_name)

    if not os.path.exists(thumb_path) or not verify_thumbnail_size(first_video['filepath']):
        generate_thumbnail(first_video['filepath'], verbose=False)
        if not os.path.exists(thumb_path) or not verify_thumbnail_size(first_video['filepath']):
            return False

    if os.path.exists(cover_path):
        if verify_cover_size(cover_path, first_video['filepath']):
            return True
        try:
            os.remove(cover_path)
        except OSError:
            pass

    try:
        import shutil
        shutil.copy2(thumb_path, cover_path)
        return True
    except OSError:
        try:
            img = cv2.imdecode(np.fromfile(thumb_path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tofile(cover_path)
                return True
        except (OSError, cv2.error):
            return False


def verify_cover_size(cover_path, video_path):
    """验证封面尺寸是否与视频缩略图一致"""
    try:
        img = cv2.imdecode(np.fromfile(cover_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return False
        h, w = img.shape[:2]
        cap = cv2.VideoCapture(video_path)
        try:
            vw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            vh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        finally:
            cap.release()
        is_landscape = vw > vh
        if is_landscape:
            expected_w, expected_h = 320, 180
        else:
            expected_w, expected_h = 180, 320
        if w == expected_w and h == expected_h:
            return True
        if w == 1 or h == 1:
            return False
        return False
    except (OSError, cv2.error):
        return False


def generate_all_series_covers(all_series, verbose=True):
    """为所有没有封面的合集生成封面图片"""
    created = 0
    for series in all_series:
        if series.get('episode_count', 0) > 1:
            video_dir = os.path.dirname(series['videos'][0]['filepath'])
            cover_path = os.path.join(video_dir, 'cover.jpg')
            if not os.path.exists(cover_path):
                if generate_series_cover(series, _verbose=verbose):
                    created += 1
    return created


def _generate_missing_assets(all_series, verbose=False):
    """扫描所有系列，为缺失缩略图或封面的视频/合集自动生成（并行生成缩略图）。

    参数:
        all_series: get_all_series() 返回的系列列表
        verbose: 是否打印详细信息

    返回:
        dict: {'thumbnails': 生成数, 'covers': 生成数, 'failed': 失败数}
    """
    stats = {'thumbnails': 0, 'covers': 0, 'failed': 0}
    # 收集所有缺失缩略图的视频路径
    missing_thumbs = []
    for series in all_series:
        videos = series.get('videos') or []
        for video in videos:
            video_path = video.get('filepath')
            if not video_path:
                continue
            video_name = os.path.splitext(os.path.basename(video_path))[0]
            thumb_name = video_name + '_thumb.jpg'
            thumb_path = os.path.join(os.path.dirname(video_path), thumb_name)
            if not os.path.exists(thumb_path):
                if _video_needs_transcode(video_path):
                    continue
                missing_thumbs.append(video_path)

    # 并行生成缩略图
    if missing_thumbs:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        workers = get_config_value("meta_workers", 4)

        def _gen_one(fp):
            return fp, generate_thumbnail(fp, verbose=verbose)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_gen_one, fp): fp for fp in missing_thumbs}
            for future in as_completed(futures):
                fp = futures[future]
                try:
                    _, result = future.result()
                except Exception:
                    result = None
                if result == 'created':
                    stats['thumbnails'] += 1
                    if verbose:
                        logger.info(f"  [补全] 缩略图: {os.path.basename(fp)}")
                elif result is None:
                    stats['failed'] += 1

    # 合集封面补全（串行，数量通常很少）
    for series in all_series:
        videos = series.get('videos') or []
        if len(videos) > 1:
            video_dir = os.path.dirname(videos[0].get('filepath', ''))
            cover_path = os.path.join(video_dir, 'cover.jpg')
            if not os.path.exists(cover_path):
                if generate_series_cover(series, _verbose=verbose):
                    stats['covers'] += 1
                    if verbose:
                        logger.info(f"  [补全] 封面: {series.get('name', '?')}")
    if stats['thumbnails'] > 0 or stats['covers'] > 0:
        save_thumbnail_cache()
    return stats


def get_thumbnail_url(series_path, video_filename):
    """根据系列路径和视频文件名生成缩略图的访问URL"""
    video_name = os.path.splitext(video_filename)[0]
    thumb_name = video_name + '_thumb.jpg'
    if series_path.startswith('abs:'):
        abs_path = series_path[4:]
        thumb_dir = abs_path if not os.path.isfile(abs_path) else os.path.dirname(abs_path)
        return f'/cover/abs/{quote(thumb_dir)}/{quote(thumb_name)}'
    if os.path.isabs(series_path):
        thumb_dir = series_path if not os.path.isfile(series_path) else os.path.dirname(series_path)
        return f'/cover/abs/{quote(thumb_dir)}/{quote(thumb_name)}'
    return f'/cover/{quote(series_path)}/{quote(thumb_name)}'


# ====================== 分类管理 ======================

# 默认分类配置常量（从配置读取，支持自定义）
def _get_default_category():
    """获取默认分类配置，支持从配置自定义"""
    default_cat = _config.get("default_category", {})
    return {
        "id": default_cat.get("id", "default"),
        "name": default_cat.get("name", "合集"),
        "icon": default_cat.get("icon", "tv"),
        "color": default_cat.get("color", "#fb7299")
    }

def _get_uncategorized_label():
    """获取未分类标签配置"""
    return _config.get("uncategorized_label", "未分类")

_DEFAULT_CATEGORY = _get_default_category()


def load_categories() -> list[dict]:
    """加载分类配置（带文件 mtime 缓存）"""
    global _categories_cache, _categories_cache_mtime
    try:
        mtime = os.path.getmtime(CATEGORIES_FILE)
    except OSError:
        mtime = 0
    if _categories_cache is not None and mtime == _categories_cache_mtime:
        return _categories_cache
    if os.path.exists(CATEGORIES_FILE):
        try:
            data = pb_utils.read_categories(CATEGORIES_FILE)
            _categories_cache = data or []
            _categories_cache_mtime = mtime
            return _categories_cache  # pyright: ignore[reportReturnType]
        except Exception as e:
            logger.info(f"[错误] 读取分类文件失败: {e}")
    # 返回默认分类
    _categories_cache = [{**_get_default_category(), "dirs": []}]
    _categories_cache_mtime = mtime
    return _categories_cache


def save_categories(categories):
    """保存分类配置"""
    global _categories_cache, _categories_cache_mtime, _allowed_dirs_cache, _allowed_dirs_cache_mtime
    global _series_list_cache, _series_list_cache_time
    try:
        pb_utils.write_categories(CATEGORIES_FILE, categories)
        _categories_cache = None  # type: ignore[reportAssignmentType]
        _categories_cache_mtime = 0
        _allowed_dirs_cache = None
        _allowed_dirs_cache_mtime = 0
        _series_list_cache = None
        _series_list_cache_time = 0
    except OSError as e:
        logger.info(f"[错误] 保存分类配置失败: {e}")
def get_video_orientation(video_path):
    """检测视频方向：返回 'landscape'(横向) 或 'portrait'(纵向)（带 mtime 缓存）"""
    cached = _get_meta_cached(video_path)
    if cached and 'orientation' in cached:
        return cached['orientation']
    cap = cv2.VideoCapture(video_path)
    try:
        if not cap.isOpened():
            return 'portrait'  # 默认竖屏
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if w <= 0 or h <= 0:
            return 'portrait'
        orient = 'landscape' if w > h else 'portrait'
        try:
            fs = os.path.getsize(video_path)
        except OSError:
            fs = 0
        _update_video_meta_cache(video_path, orientation=orient, width=w, height=h,
                                 resolution=f"{w}x{h}",
                                 file_size=fs if fs > 0 else None)
        return orient
    except Exception as e:
        logger.info(f"[警告] 检测视频方向失败: {video_path}, 错误: {e}")
        return 'portrait'
    finally:
        cap.release()


def get_series_category(dir_name):
    """获取目录所属的分类信息"""
    cats = load_categories()
    for cat in cats:
        if dir_name in (cat.get('dirs') or []):
            return cat
    return {"id": "default", "name": "合集", "icon": "tv", "color": "#fb7299", "dirs": []}


# ====================== 后台文件监控（自动检测新增目录） ======================
_known_dirs = set()  # 已知的剧集目录集合
_watcher_running = False


def _get_watched_dirs():
    """获取所有需要监控的目录集合（分类配置中的目录 + VIDEO_BASE_DIR 下的视频目录）"""
    dirs = set()
    cats = load_categories()
    for cat in cats:
        for d in (cat.get('dirs') or []):
            d_str = str(d)
            full = d_str if os.path.isabs(d_str) else os.path.join(VIDEO_BASE_DIR, d_str)
            if os.path.isdir(full):
                dirs.add(os.path.normpath(full))
    # 同时监控 VIDEO_BASE_DIR 下一级子目录
    skip = {'static', 'templates', '__pycache__', 'uploads', '.git', 'ffmpeg'}
    try:
        for item in os.listdir(VIDEO_BASE_DIR):
            if item.startswith('.') or item in skip:
                continue
            full_path = os.path.join(VIDEO_BASE_DIR, item)
            if os.path.isdir(full_path):
                dirs.add(os.path.normpath(full_path))
    except OSError:
        pass
    return dirs


def _get_dir_videos(dir_path):
    """获取目录下所有视频文件路径"""
    videos = []
    try:
        for root, _, files in os.walk(dir_path):
            for f in files:
                if not f.startswith('.'):
                    ext = os.path.splitext(f)[1].lower()
                    if ext in ALLOWED_EXTENSIONS:
                        videos.append(os.path.join(root, f))
    except (OSError, PermissionError):
        pass
    return videos


def _file_watcher_loop():
    """后台线程：定时扫描所有分类目录变化，自动更新缓存和生成缩略图"""
    global _known_dirs, _watcher_running
    interval = get_config_value("watcher_interval", 5)
    logger.info(f"[监控] 目录监控已启动，每{interval}秒检查一次...")
    while _watcher_running:
        time.sleep(interval)
        if not is_auto_scan_enabled():
            continue
        try:
            # 获取当前所有应监控的目录
            watched_dirs = _get_watched_dirs()
            new_dirs = watched_dirs - _known_dirs

            if new_dirs:
                for dir_path in sorted(new_dirs):
                    dir_name = os.path.basename(dir_path)
                    logger.info(f"[监控] 发现新目录: {dir_path}")
                    videos = _get_dir_videos(dir_path)
                    count = 0
                    for fpath in videos:
                        result = generate_thumbnail(fpath, verbose=False)
                        if result == 'created':
                            logger.info(f"  [缩略图] {os.path.basename(fpath)}")
                        elif result is None:
                            logger.info(f"  [失败] {os.path.basename(fpath)} (无法生成)")
                        count += 1
                    logger.info(f"[监控] {dir_name}: 已处理 {count} 个视频文件")
                    # 定向更新该目录的缓存
                    try:
                        cat_info = get_series_category(dir_path)
                        results = scan_dir_for_series(dir_path, cat_info, force_series=True)
                        if results:
                            update_series_cache_entry(dir_path, results)
                    except Exception as e:
                        logger.info(f"[监控] 更新缓存失败: {dir_path}, 错误: {e}")
                # 合并新发现的目录
                _known_dirs.update(new_dirs)
            else:
                # 定期刷新监控目录列表（分类配置可能变化）
                _known_dirs = watched_dirs

        except Exception as e:
            logger.info(f"[监控] 监控异常: {e}")
def start_file_watcher():
    """启动后台文件监控线程"""
    global _known_dirs, _watcher_running
    # 初始化已知目录列表（所有分类配置中的目录）
    _known_dirs = _get_watched_dirs()
    logger.info(f"[监控] 初始已识别 {len(_known_dirs)} 个目录")
    _watcher_running = True
    watcher_thread = threading.Thread(target=_file_watcher_loop, daemon=True)
    watcher_thread.start()


# ====================== 路由定义 ======================

@app.get("/favicon.ico")
async def favicon():
    """返回简单的favicon图标（粉色圆形）"""
    import base64
    png_base64 = (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAOklEQVQ4T2NkYGD4z0ABYCRSMGwMOg8M"
        "BgwMDCH6IEUbRBvA4INoA8F8MDRBhAFyGUSPIWoDyHYQNYDJBzL5QKYAAQYAG/coJEpDq9YAAAAASUVO"
        "RK5CYII="
    )
    png_data = base64.b64decode(png_base64)
    return Response(content=png_data, media_type='image/png')

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, cat: str = Query(None), sort: str = Query("default")):  # pyright: ignore[reportCallInDefaultInitializer]

    """首页：显示所有我的影院系列"""
    series_list = await asyncio.get_event_loop().run_in_executor(None, get_all_series)

    # 排序
    if sort == "name":
        series_list.sort(key=lambda s: s['name'].lower())
    elif sort == "name_desc":
        series_list.sort(key=lambda s: s['name'].lower(), reverse=True)
    elif sort == "episodes":
        series_list.sort(key=lambda s: s.get('total_episodes', 0) or s.get('episode_count', 0))
    elif sort == "episodes_desc":
        series_list.sort(key=lambda s: s.get('total_episodes', 0) or s.get('episode_count', 0), reverse=True)
    elif sort == "updated":
        def get_cached_mtime(s):
            p = s.get('_abs_path') or os.path.join(VIDEO_BASE_DIR, s['path'])
            cached = _series_cache.get(p)
            return cached.get('mtime', 0) if cached else 0
        series_list.sort(key=get_cached_mtime, reverse=True)

    # 按分类分组（按分类管理顺序）
    categories = load_categories()
    cat_order = {cat['id']: i for i, cat in enumerate(categories)}
    
    categorized = {}
    unassigned = []
    for s in series_list:
        cat_id = s['category'].get('id', 'default')
        if cat_id == 'default' and len(s['category'].get('dirs') or []) == 0:
            # 未分类的系列
            unassigned.append(s)
        elif cat_id not in categorized:
            categorized[cat_id] = {'category': s['category'], 'series': []}
            categorized[cat_id]['series'].append(s)
        else:
            categorized[cat_id]['series'].append(s)
    
    # 按分类管理顺序排序
    grouped = {}
    for cat in categories:
        cat_id = cat['id']
        if cat_id in categorized:
            grouped[cat_id] = categorized[cat_id]

    # 统计总集数
    total_episodes = sum(s['episode_count'] for s in series_list)

    # 计算未分类的剧集（属于默认分类但未手动分配到任何分类的目录）
    unassigned_series = [s for s in series_list
                         if s['category'].get('id') == 'default'
                         and len(s['category'].get('dirs') or []) == 0]

    # 构建分类统计
    cat_counts = {}
    for cat in load_categories():
        if not isinstance(cat, dict):
            continue
        cat_id: str = str(cat.get('id', 'default'))
        cat_counts[cat_id] = {
            'count': len([s for s in series_list if s['category'].get('id') == cat_id]),
            'episodes': sum(s['episode_count'] for s in series_list if s['category'].get('id') == cat_id)
        }

    # 预筛选分类（用于前端自动选中分类标签）
    preselected_cat = cat or ""

    return render_template("index.html",
                           request=request,
                           series_list=series_list,
                           grouped=grouped,
                           categories=load_categories(),
                           total_episodes=total_episodes,
                           unassigned_series=unassigned_series,
                           cat_counts=cat_counts,
                           preselected_cat=preselected_cat,
                           current_sort=sort,
                           page_title="首页 - 我的影院")


@app.get("/api/browse")
async def api_browse_dir(request: Request, path: str = "", allow_all: bool = False):
    """浏览目录内容（返回子目录列表），allow_all 仅限本地访问"""
    # allow_all 仅允许本地访问，防止公网暴露文件系统
    if allow_all and (not request.client or request.client.host not in ('127.0.0.1', '::1', 'localhost')):
        allow_all = False
    target_path = path if path else VIDEO_BASE_DIR
    if not os.path.isabs(target_path):
        target_path = os.path.join(VIDEO_BASE_DIR, target_path)

    if not allow_all and not _is_abs_path_allowed(target_path):
        return JSONResponse({"ok": False, "error": "禁止访问该目录"}, status_code=403)

    try:
        entries = []
        for item in sorted(os.listdir(target_path)):
            full = os.path.join(target_path, item)
            if os.path.isdir(full) and not item.startswith('.'):
                entries.append({"name": item, "path": full})

        parent = os.path.dirname(target_path) if target_path else None

        return JSONResponse({"ok": True, "path": target_path, "parent": parent, "entries": entries})
    except OSError:
        return JSONResponse({"ok": False, "error": "无权限访问该目录"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/drives")
async def api_get_drives():
    """获取系统所有可用磁盘"""
    drives = []
    if os.name == 'nt':
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            if bitmask & 1:
                drives.append(letter + ":\\")
            bitmask >>= 1
    else:
        drives.append("/")
    return JSONResponse(drives)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """设置页：管理视频分类"""
    cats = load_categories()
    available_dirs = get_available_dirs()
    # 检测分类关联的目录中不存在的路径，并计算目录类型
    nonexistent_dirs = []
    dir_types = {}
    for cat in cats:
        for d in (cat.get('dirs') or []):
            d_str = str(d)
            full = d_str if os.path.isabs(d_str) else os.path.join(VIDEO_BASE_DIR, d_str)
            if not os.path.isdir(full):
                nonexistent_dirs.append(d_str)
                continue
            # 检测目录类型
            has_videos = False
            has_video_subdirs = False
            try:
                for item in os.listdir(full):
                    fpath = os.path.join(full, item)
                    if os.path.isfile(fpath) and not item.startswith('.'):
                        ext = os.path.splitext(item)[1].lower()
                        if ext in ALLOWED_EXTENSIONS:
                            has_videos = True
                    elif os.path.isdir(fpath) and not item.startswith('.'):
                        if not has_video_subdirs:
                            try:
                                for sub_f in os.listdir(fpath):
                                    if os.path.splitext(sub_f)[1].lower() in ALLOWED_EXTENSIONS:
                                        has_video_subdirs = True
                                        break
                            except OSError:
                                pass
            except OSError:
                pass
            if has_videos and has_video_subdirs:
                dir_types[d_str] = "混合"
            elif not has_videos and has_video_subdirs:
                dir_types[d_str] = "合集"
            elif has_videos and not has_video_subdirs:
                dir_types[d_str] = "扁平"
            else:
                dir_types[d_str] = "空"
    return render_template("settings.html",
                           request=request,
                           categories_json=json.dumps({"categories": cats}, ensure_ascii=False),
                           available_dirs=available_dirs,
                           nonexistent_dirs=json.dumps(nonexistent_dirs, ensure_ascii=False),
                           dir_types_json=json.dumps(dir_types, ensure_ascii=False),
                           video_base_dir=VIDEO_BASE_DIR,
                           auto_scan_enabled=is_auto_scan_enabled(),
                           page_title="分类设置 - 我的影院")


@app.post("/api/categories")
async def api_save_categories(request: Request):
    """保存分类配置（AJAX接口）"""
    global _series_list_cache, _series_list_cache_time
    
    try:
        body = await request.json()
        categories = body.get('categories', [])
        save_categories(categories)

        # 清除目录级缓存（触发目录内增量扫描），但不清除列表缓存
        global _series_cache
        with _series_list_cache_lock:
            _series_cache = {}

        force_scan = body.get('force_scan', False)
        if force_scan or is_auto_scan_enabled():
            def _bg_update():
                try:
                    # 使用增量更新：先扫描，再合并，不阻塞用户
                    new_series_list = _get_all_series_uncached()
                    with _series_list_cache_lock:
                        _incremental_merge_series(new_series_list)
                        _series_list_cache_time = time.time()
                    save_series_cache()
                    logger.info(f"[分类扫描] 后台增量扫描完成：{len(_series_list_cache)} 个条目")
                    _do_auto_export()
                except Exception as e:
                    logger.info(f"[分类扫描] 后台扫描异常: {e}")
            threading.Thread(target=_bg_update, daemon=True).start()

        return JSONResponse({"ok": True})
    except Exception as e:
        logger.info(f"[错误] 保存分类失败: {e}")
        logger.exception("")
        return JSONResponse({"ok": False, "error": str(e)})


def _do_auto_export():
    """扫描完成后自动导出备份到 backup_latest 目录"""
    import datetime
    import shutil
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = os.path.join(BASE_DIR, "backup_latest")
    try:
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir)
        os.makedirs(backup_dir, exist_ok=True)
        categories = load_categories()
        export_data = {"categories": categories, "auto_scan_enabled": is_auto_scan_enabled(), "export_time": timestamp}
        pb_utils.write_config(os.path.join(backup_dir, "config.pb"), _config)
        pb_utils.write_categories(os.path.join(backup_dir, "categories.pb"), categories)
        pb_utils.write_thumbnail_cache(os.path.join(backup_dir, "thumbnail_cache.pb"), load_thumbnail_cache_data())
        pb_utils.write_video_meta_cache(os.path.join(backup_dir, "video_meta_cache.pb"), _video_meta_cache)
        hidden_data = pb_utils.read_hidden_series(_hidden_series_file) if os.path.exists(_hidden_series_file) else []
        pb_utils.write_hidden_series(os.path.join(backup_dir, "hidden_series.pb"), hidden_data)
        series_data = pb_utils.read_series_cache(SERIES_CACHE_FILE) if os.path.exists(SERIES_CACHE_FILE) else {}
        pb_utils.write_series_cache(os.path.join(backup_dir, "series_cache.pb"), series_data)
        pb_utils.write_export_info(os.path.join(backup_dir, "export_info.pb"), export_data)
        logger.info(f"[自动备份] 已备份到: {backup_dir}")
    except Exception as e:
        logger.info(f"[自动备份] 失败: {e}")
@app.get("/api/categories/export")
async def api_export_categories():
    """导出分类配置及缓存到程序目录"""
    import datetime
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = os.path.join(BASE_DIR, "backup_latest")
    
    try:
        if os.path.exists(backup_dir):
            import shutil
            shutil.rmtree(backup_dir)
        
        os.makedirs(backup_dir, exist_ok=True)
        
        categories = load_categories()
        export_data = {
            "categories": categories,
            "auto_scan_enabled": is_auto_scan_enabled(),
            "export_time": timestamp
        }
        
        pb_utils.write_config(os.path.join(backup_dir, "config.pb"), _config)
        pb_utils.write_categories(os.path.join(backup_dir, "categories.pb"), categories)
        pb_utils.write_thumbnail_cache(os.path.join(backup_dir, "thumbnail_cache.pb"), load_thumbnail_cache_data())
        pb_utils.write_video_meta_cache(os.path.join(backup_dir, "video_meta_cache.pb"), _video_meta_cache)
        
        if os.path.exists(_hidden_series_file):
            hidden_data = pb_utils.read_hidden_series(_hidden_series_file)
        else:
            hidden_data = []
        pb_utils.write_hidden_series(os.path.join(backup_dir, "hidden_series.pb"), hidden_data)
        
        if os.path.exists(SERIES_CACHE_FILE):
            series_data = pb_utils.read_series_cache(SERIES_CACHE_FILE)
        else:
            series_data = {}
        pb_utils.write_series_cache(os.path.join(backup_dir, "series_cache.pb"), series_data)
        
        pb_utils.write_export_info(os.path.join(backup_dir, "export_info.pb"), export_data)
        
        logger.info(f"[导出] 已导出到: {backup_dir}")
        return JSONResponse({"ok": True, "path": backup_dir, "timestamp": timestamp})
    except Exception as e:
        logger.info(f"[错误] 导出失败: {e}")
        return JSONResponse({"ok": False, "error": str(e)})


def load_thumbnail_cache_data():
    """加载缩略图缓存数据"""
    if os.path.exists(THUMBNAIL_CACHE_FILE):
        try:
            return pb_utils.read_thumbnail_cache(THUMBNAIL_CACHE_FILE)
        except Exception:
            pass
    return {}


@app.post("/api/categories/import")
async def api_import_categories(request: Request):
    """从程序目录导入分类配置及缓存"""
    global _series_list_cache, _series_list_cache_time, _video_meta_cache, _hidden_series
    
    latest_dir = os.path.join(BASE_DIR, "backup_latest")
    
    if not os.path.exists(latest_dir):
        return JSONResponse({"ok": False, "error": "没有找到备份目录，请先导出"})
    
    try:
        def _read_pb_or_json_file(filepath, pb_reader_fn):
            """优先读取 Protobuf 文件，不存在时尝试 JSON 格式（兼容旧备份）"""
            pb_path = filepath if filepath.endswith('.pb') else filepath.replace('.json', '.pb')
            json_path = filepath if filepath.endswith('.json') else filepath.replace('.pb', '.json')
            # 优先 pb
            if os.path.exists(pb_path):
                try:
                    return pb_reader_fn(pb_path)
                except Exception:
                    pass
            # 兼容旧 json
            if os.path.exists(json_path):
                try:
                    with open(json_path, 'r', encoding='utf-8') as f:
                        return json.load(f)
                except Exception:
                    pass
            return None
        
        def _restore_pb_file(filepath, data, writer_fn):
            """以 Protobuf 格式恢复文件"""
            if data is not None:
                try:
                    writer_fn(filepath, data)
                    return True
                except Exception as e:
                    logger.info(f"[导入] 恢复文件失败 {filepath}: {e}")
            return False
        
        restored_count = 0
        
        config_data = _read_pb_or_json_file(
            os.path.join(latest_dir, "config.json"), pb_utils.read_config)
        if config_data:
            if _restore_pb_file(os.path.join(BASE_DIR, "config.pb"), config_data, pb_utils.write_config):
                restored_count += 1
        
        thumb_data = _read_pb_or_json_file(
            os.path.join(latest_dir, "thumbnail_cache.json"), pb_utils.read_thumbnail_cache)
        if thumb_data:
            if _restore_pb_file(THUMBNAIL_CACHE_FILE, thumb_data, pb_utils.write_thumbnail_cache):
                restored_count += 1
        
        meta_data = _read_pb_or_json_file(
            os.path.join(latest_dir, "video_meta_cache.json"), pb_utils.read_video_meta_cache)
        if meta_data:
            if _restore_pb_file(_video_meta_cache_file, meta_data, pb_utils.write_video_meta_cache):
                restored_count += 1
        
        hidden_data = _read_pb_or_json_file(
            os.path.join(latest_dir, "hidden_series.json"), pb_utils.read_hidden_series)
        if hidden_data:
            if _restore_pb_file(_hidden_series_file, hidden_data, pb_utils.write_hidden_series):
                _hidden_series = set(hidden_data if isinstance(hidden_data, list) else hidden_data.get('paths', []))
                restored_count += 1
        
        series_data = _read_pb_or_json_file(
            os.path.join(latest_dir, "series_cache.json"), pb_utils.read_series_cache)
        if series_data:
            if _restore_pb_file(SERIES_CACHE_FILE, series_data, pb_utils.write_series_cache):
                restored_count += 1
        
        categories = _read_pb_or_json_file(
            os.path.join(latest_dir, "categories.json"), pb_utils.read_categories)
        if not categories:
            export_info = _read_pb_or_json_file(
                os.path.join(latest_dir, "export_info.json"), pb_utils.read_export_info)
            if export_info:
                categories = export_info.get('categories', [])
        
        if not categories:
            return JSONResponse({"ok": False, "error": "未找到分类数据"})
        
        save_categories(categories)
        
        info_data = _read_pb_or_json_file(
            os.path.join(latest_dir, "export_info.json"), pb_utils.read_export_info)
        if info_data and 'auto_scan_enabled' in info_data:
            set_auto_scan_enabled(bool(info_data['auto_scan_enabled']))
        
        if restored_count > 0:
            try:
                load_series_cache()
                load_thumbnail_cache()
                _load_video_meta_cache()
                load_hidden_series()
                logger.info("[导入] 缓存已重新加载到内存")
            except Exception as e:
                logger.info(f"[导入] 重新加载缓存失败: {e}")
        logger.info(f"[导入] 已恢复 {restored_count} 个缓存文件")
        return JSONResponse({"ok": True, "count": len(categories), "restored": restored_count})
    except Exception as e:
        logger.info(f"[错误] 导入分类失败: {e}")
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/auto-scan")
async def api_get_auto_scan():
    """获取自动扫描开关状态"""
    return JSONResponse({"enabled": is_auto_scan_enabled()})

@app.post("/api/auto-scan")
async def api_set_auto_scan(request: Request):
    """设置自动扫描开关状态"""
    try:
        data = await request.json()
        enabled = bool(data.get("enabled", True))
        set_auto_scan_enabled(enabled)
        logger.info(f"[设置] 自动扫描已{'开启' if enabled else '关闭'}")
        return JSONResponse({"ok": True, "enabled": enabled})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/category/rescan")
async def api_rescan_category(request: Request):
    """重新扫描指定分类 - 同步扫描，对比差异后更新"""
    try:
        global _series_list_cache, _series_list_cache_time

        data = await request.json()
        category_id = data.get("category_id")

        if not category_id:
            return JSONResponse({"ok": False, "error": "缺少分类ID"})

        cats = load_categories()
        target_cat = None
        for cat in cats:
            if cat.get("id") == category_id:
                target_cat = cat
                break

        if not target_cat:
            return JSONResponse({"ok": False, "error": "分类不存在"})

        cat_info = {
            "id": target_cat.get('id'),
            "name": target_cat.get('name'),
            "icon": target_cat.get('icon', _get_default_category().get('icon')),
            "color": target_cat.get('color', _get_default_category().get('color')),
            "dirs": target_cat.get('dirs', [])
        }

        logger.info(f"[分类扫描] 开始扫描分类: {target_cat.get('name')}")
        dirs = target_cat.get('dirs') or []

        existing_series_map = {}
        if _series_list_cache is not None:
            for s in _series_list_cache:
                if s.get('category', {}).get('id') == category_id:
                    existing_series_map[s.get('path')] = s

        def _has_direct_videos(path):
            try:
                for item in os.listdir(path):
                    if os.path.isfile(os.path.join(path, item)):
                        ext = os.path.splitext(item)[1].lower()
                        if ext in ALLOWED_EXTENSIONS:
                            return True
            except PermissionError:
                pass
            return False

        def _has_video_subdirs(path):
            try:
                for item in os.listdir(path):
                    sub = os.path.join(path, item)
                    if os.path.isdir(sub) and _has_direct_videos(sub):
                        return True
            except PermissionError:
                pass
            return False

        new_count = 0
        updated_count = 0
        removed_count = 0
        scanned_paths = set()

        for d in dirs:
            d_str = str(d)
            full_path = d_str if os.path.isabs(d_str) else os.path.join(VIDEO_BASE_DIR, d_str)

            if not os.path.isdir(full_path):
                for path in list(existing_series_map.keys()):
                    if path.startswith(full_path):
                        del existing_series_map[path]
                        removed_count += 1
                continue

            has_videos = _has_direct_videos(full_path)
            has_video_subdirs = _has_video_subdirs(full_path)

            if not has_videos and has_video_subdirs:
                try:
                    current_subdirs = set()
                    for item in os.listdir(full_path):
                        sub_path = os.path.join(full_path, item)
                        if os.path.isdir(sub_path) and _has_direct_videos(sub_path):
                            current_subdirs.add(sub_path)
                except PermissionError:
                    continue

                for sub_path in current_subdirs:
                    scan_results = []
                    scan_dir_recursive(sub_path, cat_info, None, scan_results, force_series=True)
                    for s in scan_results:
                        s_path = s.get('path')
                        scanned_paths.add(s_path)
                        if s_path in existing_series_map:
                            updated_count += 1
                        else:
                            new_count += 1
                        existing_series_map[s_path] = s

                for path in list(existing_series_map.keys()):
                    if path.startswith(full_path) and path not in scanned_paths:
                        # 检查文件是否存在，或者对于目录类型的条目，检查是否还有视频
                        if os.path.isdir(path):
                            # 如果是目录（合集容器），检查是否还有视频
                            if not _has_direct_videos(path) and not _has_video_subdirs(path):
                                del existing_series_map[path]
                                removed_count += 1
                        elif not os.path.exists(path):
                            del existing_series_map[path]
                            removed_count += 1

            elif has_videos or has_video_subdirs:
                scan_results = []
                scan_dir_recursive(full_path, cat_info, None, scan_results, force_series=True)

                scanned_in_dir = set()
                for s in scan_results:
                    s_path = s.get('path')
                    scanned_paths.add(s_path)
                    scanned_in_dir.add(s_path)
                    if s_path in existing_series_map:
                        updated_count += 1
                    else:
                        new_count += 1
                    existing_series_map[s_path] = s

                for path in list(existing_series_map.keys()):
                    if path.startswith(full_path) and path not in scanned_in_dir:
                        # 检查文件是否存在，或者对于目录类型的条目，检查是否还有视频
                        if os.path.isdir(path):
                            # 如果是目录（合集容器），检查是否还有视频
                            if not _has_direct_videos(path) and not _has_video_subdirs(path):
                                del existing_series_map[path]
                                removed_count += 1
                        elif not os.path.exists(path):
                            del existing_series_map[path]
                            removed_count += 1

        for path in list(existing_series_map.keys()):
            if not os.path.exists(path):
                del existing_series_map[path]
                removed_count += 1

        if _series_list_cache is not None:
            other_series = [s for s in _series_list_cache if s.get('category', {}).get('id') != category_id]
            new_cache = other_series + list(existing_series_map.values())
        else:
            new_cache = list(existing_series_map.values())

        with _series_list_cache_lock:
            _series_list_cache = new_cache
            _series_list_cache_time = time.time()
        save_series_cache()

        logger.info(f"[分类扫描] 完成：{new_count} 新增，{updated_count} 更新，{removed_count} 删除，共 {len(_series_list_cache)} 条")

        return JSONResponse({
            "ok": True,
            "added": new_count,
            "updated": updated_count,
            "removed": removed_count,
            "total": len(_series_list_cache),
            "category": target_cat.get('name')
        })
    except Exception as e:
        logger.info(f"[错误] 分类扫描失败: {e}")
        logger.exception("")
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/config")
async def api_get_config():
    """获取高级设置配置"""
    return JSONResponse({
        "ok": True,
        "config": {
            "delete_original_after_transcode": _config.get("delete_original_after_transcode", True),
            "auto_transcode": _config.get("auto_transcode", True),
            "idle_check_interval": int(_config.get("idle_check_interval", 300)),
            "watcher_interval": int(_config.get("watcher_interval", 5)),
            "cache_ttl": int(_config.get("cache_ttl", 300)),
            "meta_workers": int(_config.get("meta_workers", 4)),
            "detail_workers": int(_config.get("detail_workers", 8)),
            "meta_cache_max": int(_config.get("meta_cache_max", 5000)),
            "gpu_transcode": bool(_config.get("gpu_transcode", False)),
            "server_port": int(_config.get("server_port", 5000)),
            "server_host": str(_config.get("server_host", "0.0.0.0")),
            "nvenc_available": _detect_nvenc(),
        }
    })

@app.post("/api/config")
async def api_set_config(request: Request):
    """保存高级设置配置"""
    global SERIES_CACHE_TTL, _VIDEO_META_CACHE_MAX
    try:
        data = await request.json()
        config = data.get("config", {})
        updated = {}

        # 转码后删除原文件
        if "delete_original_after_transcode" in config:
            val = bool(config["delete_original_after_transcode"])
            _config["delete_original_after_transcode"] = val
            updated["delete_original_after_transcode"] = val

        # 自动转码为 MP4
        if "auto_transcode" in config:
            val = bool(config["auto_transcode"])
            _config["auto_transcode"] = val
            updated["auto_transcode"] = val

        # GPU 转码开关
        if "gpu_transcode" in config:
            val = bool(config["gpu_transcode"])
            if val and not _detect_nvenc():
                return JSONResponse({"ok": False, "error": "未检测到 NVENC 支持，无法启用 GPU 转码"})
            _config["gpu_transcode"] = val
            updated["gpu_transcode"] = val

        # 高级设置 key 范围校验
        allowed_keys = {
            "idle_check_interval": (10, 3600),
            "watcher_interval": (1, 60),
            "cache_ttl": (30, 3600),
            "meta_workers": (1, 16),
            "detail_workers": (1, 16),
            "meta_cache_max": (100, 50000),
            "server_port": (1, 65535),
        }
        for key, (min_val, max_val) in allowed_keys.items():
            if key in config:
                val = int(config[key])
                val = max(min_val, min(max_val, val))
                _config[key] = val
                updated[key] = val
        
        # 服务器主机地址
        if "server_host" in config:
            val = str(config["server_host"]).strip()
            if val:
                _config["server_host"] = val
                updated["server_host"] = val

        # 更新全局变量
        if "cache_ttl" in updated:
            SERIES_CACHE_TTL = updated["cache_ttl"]
        if "meta_cache_max" in updated:
            _VIDEO_META_CACHE_MAX = updated["meta_cache_max"]

        save_config(_config)
        logger.info(f"[设置] 配置已更新: {updated}")
        return JSONResponse({"ok": True, "updated": updated})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/server-info")
async def api_server_info():
    """获取服务器信息"""
    import platform
    global _server_start_time
    uptime = int(time.time() - _server_start_time) if _server_start_time else 0
    # 计算运行时间
    days = uptime // 86400
    hours = (uptime % 86400) // 3600
    minutes = (uptime % 3600) // 60
    seconds = uptime % 60
    uptime_str = f"{days}天{hours}时{minutes}分{seconds}秒" if days > 0 else f"{hours}时{minutes}分{seconds}秒"

    # 检查 FFmpeg
    ffmpeg_ok = False
    try:
        result = subprocess.run([get_ffmpeg_cmd(), '-version'], capture_output=True, timeout=5)
        ffmpeg_ok = result.returncode == 0
    except:
        pass

    # 系统信息
    gpu_percent = None
    gpu_name = ''
    if PSUTIL_AVAILABLE:
        try:
            _cc = psutil.cpu_count()
            cpu_count = _cc if _cc else psutil.cpu_count(logical=False) or '?'
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage(VIDEO_BASE_DIR[:3] if len(VIDEO_BASE_DIR) >= 3 else '.')
            cpu_percent = psutil.cpu_percent(interval=0.1)
        except Exception:
            cpu_count = '?'
            memory = None
            disk = None
            cpu_percent = 0
    else:
        cpu_count = '?'
        memory = None
        disk = None
        cpu_percent = 0

    # GPU 信息（支持多显卡，优先显示独显）
    gpu_encoder_percent = None  # NVENC 编码器利用率
    if PSUTIL_AVAILABLE:
        try:
            import subprocess as _subprocess
            # 查询所有GPU的名称、3D利用率、编码器利用率、显存总量
            result = _subprocess.run(
                ['nvidia-smi', '--query-gpu=name,utilization.gpu,utilization.encoder,memory.total',
                 '--format=csv,noheader'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split('\n')
                gpus = []
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    parts = [p.strip() for p in line.split(',')]
                    if len(parts) >= 4:
                        # 使用率可能带 " %" 后缀，显存可能带 " MiB" 单位，均提取数字
                        _re_mod = __import__('re')
                        def _extract_num(s):
                            m = _re_mod.search(r'[\d.]+', s or '')
                            return float(m.group()) if m else 0
                        gpus.append({
                            'name': parts[0],
                            'util': _extract_num(parts[1]),       # 3D/CUDA 利用率
                            'enc_util': _extract_num(parts[2]),   # 编码器(NVENC)利用率
                            'vram_mb': int(_extract_num(parts[3])),
                        })
                if gpus:
                    # 优先选择独显：显存最大且名称不含集成显卡关键词
                    integrated_keywords = ('intel', 'uhd', 'iris', 'hd graphics', 'integrated')
                    dedicated_gpus = [g for g in gpus if not any(kw in g['name'].lower() for kw in integrated_keywords)]
                    target = max(dedicated_gpus if dedicated_gpus else gpus, key=lambda g: g['vram_mb'])
                    gpu_percent = target['util']
                    gpu_encoder_percent = target['enc_util']
                    gpu_name = target['name']
        except Exception:
            pass

    # L1 内存缓存统计
    with _video_content_cache_lock:
        content_cache_entries = len(_video_content_cache)
        content_cache_size = sum(e["size"] for e in _video_content_cache.values())
        content_cache_ratio = content_cache_size / VIDEO_CONTENT_CACHE_MAX_SIZE * 100 if VIDEO_CONTENT_CACHE_MAX_SIZE > 0 else 0

    # 预加载队列统计
    with _prefetch_lock:
        prefetch_queue_size = len(_prefetch_queue)

    return JSONResponse({
        "ok": True,
        "info": {
            "version": "1.1.0",
            "uptime": uptime_str,
            "uptime_seconds": uptime,
            "ffmpeg_ok": ffmpeg_ok,
            "nvenc_available": _detect_nvenc(),
            "gpu_transcode_enabled": bool(_config.get("gpu_transcode", False)),
            "cpu_count": cpu_count,
            "cpu_percent": cpu_percent,
            "gpu_percent": gpu_percent,
            "gpu_encoder_percent": gpu_encoder_percent,
            "gpu_name": gpu_name,
            "memory_total": getattr(memory, 'total', 0),
            "memory_used": getattr(memory, 'used', 0),
            "memory_percent": getattr(memory, 'percent', 0),
            "disk_total": getattr(disk, 'total', 0),
            "disk_used": getattr(disk, 'used', 0),
            "disk_percent": getattr(disk, 'percent', 0),
            "python_version": platform.python_version(),
            "l1_cache_entries": content_cache_entries,
            "l1_cache_size": content_cache_size,
            "l1_cache_ratio": round(content_cache_ratio, 1),
            "prefetch_queue_size": prefetch_queue_size,
        }
    })


@app.post("/api/cache/clear")
async def api_clear_cache(request: Request):
    """清理各类缓存"""
    try:
        data = await request.json()
        cleared = []
        # 清理缩略图缓存
        if data.get("thumbnail_cache"):
            try:
                with open(THUMBNAIL_CACHE_FILE, 'w', encoding='utf-8') as f:
                    json.dump({}, f)
                cleared.append("缩略图缓存")
            except:
                pass
        # 清理视频元数据缓存
        if data.get("video_meta_cache"):
            global _video_meta_cache
            try:
                with open(_video_meta_cache_file, 'w', encoding='utf-8') as f:
                    json.dump({}, f)
                with _video_meta_cache_lock:
                    _video_meta_cache = {}
                cleared.append("视频元数据缓存")
            except:
                pass
        # 清理系列缓存
        if data.get("series_cache"):
            global _series_cache, _series_list_cache, _series_list_cache_time
            try:
                if os.path.exists(SERIES_CACHE_FILE):
                    os.remove(SERIES_CACHE_FILE)
                _series_cache = {}
                _series_list_cache = None
                _series_list_cache_time = 0
                cleared.append("系列缓存")
            except:
                pass
        # 清理 L1 内存缓存
        if data.get("content_cache"):
            global _video_content_cache
            with _video_content_cache_lock:
                cache_size = sum(e["size"] for e in _video_content_cache.values())
                _video_content_cache.clear()
            cleared.append(f"L1 内存缓存 ({cache_size / 1024 / 1024:.1f}MB)")
            logger.info(f"[缓存 L1] 已清空 ({cache_size / 1024 / 1024:.1f}MB)")
        return JSONResponse({"ok": True, "cleared": cleared})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/hidden-series")
async def api_get_hidden_series():
    """获取隐藏的视频列表"""
    try:
        with _hidden_series_lock:
            hidden_list = sorted(_hidden_series)
        # 获取每个隐藏视频的信息
        result = []
        for path in hidden_list:
            if os.path.exists(path):
                mtime = os.path.getmtime(path)
                size = os.path.getsize(path)
                result.append({
                    "path": path,
                    "name": os.path.basename(os.path.dirname(path)) or os.path.basename(path),
                    "mtime": mtime,
                    "size": size
                })
        return JSONResponse({"ok": True, "list": result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/hidden-series/restore")
async def api_restore_hidden_series(request: Request):
    """恢复隐藏的视频"""
    try:
        data = await request.json()
        paths = data.get("paths", [])
        restored = 0
        with _hidden_series_lock:
            for path in paths:
                if path in _hidden_series:
                    _hidden_series.discard(path)
                    restored += 1
        if restored > 0:
            save_hidden_series()
        return JSONResponse({"ok": True, "restored": restored})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/hidden-series/clear")
async def api_clear_hidden_series(request: Request):
    """清空所有隐藏的视频"""
    try:
        data = await request.json()
        paths_to_remove = data.get("paths", [])
        removed = 0
        with _hidden_series_lock:
            for path in paths_to_remove:
                if path in _hidden_series:
                    _hidden_series.discard(path)
                    removed += 1
        if removed > 0:
            save_hidden_series()
        return JSONResponse({"ok": True, "removed": removed})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/transcode")
async def api_get_transcode_status():
    """获取转码队列状态"""
    try:
        with _transcode_lock:
            queue = list(_transcode_queue)
            in_progress = list(_transcode_in_progress.keys())
            progress = dict(_transcode_progress)
        pending_count = len(queue)
        in_progress_count = len(in_progress)
        total = pending_count + in_progress_count
        return JSONResponse({
            "ok": True,
            "total": total,
            "pending": pending_count,
            "in_progress": in_progress_count,
            "queue": queue[:20],
            "in_progress_list": in_progress[:20],
            "progress": progress
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/transcode/cancel")
async def api_cancel_transcode(request: Request):
    """取消指定的转码任务"""
    try:
        data = await request.json()
        video_path = data.get("path", "")
        with _transcode_lock:
            if video_path in _transcode_queue:
                _transcode_queue.remove(video_path)
                removed = True
            else:
                removed = False
        if removed:
            _remove_pending_transcode(video_path)
            logger.info(f"[转码管理] 已取消: {os.path.basename(video_path)}")
            return JSONResponse({"ok": True, "removed": True})
        return JSONResponse({"ok": True, "removed": False, "message": "任务不在队列中"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/transcode/clear-done")
async def api_clear_done_transcode():
    """清空已完成记录"""
    try:
        with _transcode_lock:
            _transcode_in_progress.clear()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/categories/clear")
async def api_clear_categories():
    """清空所有分类"""
    save_categories([])
    _series_cache.clear()
    _clear_series_cache_file()
    return JSONResponse({"ok": True})





@app.get("/api/meta/stats")
async def api_get_meta_stats():
    """获取元数据缓存统计"""
    try:
        all_series = get_all_series()
        total = 0
        cached = 0
        seen = set()
        for series in all_series:
            for v in (series.get('videos') or []):
                fp = v.get('filepath')
                if fp and fp not in seen:
                    seen.add(fp)
                    total += 1
                    c = _get_meta_cached(fp)
                    if c and c.get('file_size', 0) > 0:
                        cached += 1
        return JSONResponse({"ok": True, "cached": cached, "total": total})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/meta/status")
async def api_get_meta_status():
    """获取元数据补全进度"""
    try:
        # 统计有效缓存数（file_size > 0）
        with _video_meta_cache_lock:
            cached = sum(1 for e in _video_meta_cache.values() if e.get('file_size', 0) > 0)
        
        # 如果没有 series 数据（自动扫描关闭且无缓存），尝试从 video_meta_cache 估算总数
        all_series = get_all_series()
        if all_series:
            total = 0
            seen = set()
            for series in all_series:
                for v in (series.get('videos') or []):
                    fp = v.get('filepath')
                    if fp and fp not in seen:
                        seen.add(fp)
                        total += 1
        else:
            # 无 series 数据时，使用 video_meta_cache 中的数据作为已缓存数
            total = cached
        
        return JSONResponse({
            "ok": True,
            "running": _meta_populate_progress["running"],
            "total": _meta_populate_progress["total"] or total,
            "done": _meta_populate_progress["done"],
            "current": _meta_populate_progress.get("current", ""),
            "cached": cached,
            "error": _meta_populate_progress.get("error"),
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/cover/generate")
async def api_generate_cover(request: Request):
    """后台生成缩略图（前端 AJAX 调用）"""
    try:
        from urllib.parse import unquote
        body = await request.json()
        dir_path = unquote(body.get('dir', ''))
        video_name = unquote(body.get('videoName', ''))
        is_abs = body.get('isAbs', False)
        
        logger.info(f"[缩略图生成] dir={dir_path}, video={video_name}, isAbs={is_abs}")
        
        if is_abs:
            video_dir = dir_path
        else:
            video_dir = os.path.join(VIDEO_BASE_DIR, dir_path) if dir_path else VIDEO_BASE_DIR
        
        thumb_path = os.path.join(video_dir, video_name + '_thumb.jpg')
        logger.info(f"[缩略图生成] thumb_path={thumb_path}")
        
        if os.path.exists(thumb_path):
            logger.info(f"[缩略图生成] 缩略图已存在: {thumb_path}")
            return JSONResponse({"ok": True, "message": "缩略图已存在"})
        
        # 查找对应视频文件
        video_path = None
        if os.path.isdir(video_dir):
            for f in os.listdir(video_dir):
                if f.startswith(video_name) and f.endswith(('.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.webm')):
                    video_path = os.path.join(video_dir, f)
                    logger.info(f"[缩略图生成] 找到视频: {video_path}")
                    break
        
        if not video_path:
            logger.warning(f"[缩略图生成] 未找到视频文件，目录内容: {os.listdir(video_dir)[:10] if os.path.isdir(video_dir) else '目录不存在'}")
            return JSONResponse({"ok": False, "error": "未找到对应视频文件"})
        
        result = generate_thumbnail(video_path, verbose=False)
        logger.info(f"[缩略图生成] 生成结果: {result}")
        if result in ('created', 'cached'):
            return JSONResponse({"ok": True, "message": "缩略图已生成"})
        else:
            return JSONResponse({"ok": False, "error": "缩略图生成失败"})
    except Exception as e:
        logger.exception(f"[缩略图生成] 异常: {e}")
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/meta/populate")
@app.post("/api/meta/populate")
async def api_populate_meta():
    """手动触发元数据补全"""
    try:
        if _is_meta_populate_running():
            return JSONResponse({"ok": True, "message": "补全正在进行中", "already_running": True})
        if not _start_meta_populate():
            return JSONResponse({"ok": True, "message": "补全正在进行中", "already_running": True})

        def _run():
            populate_all_video_meta()

        threading.Thread(target=_run, daemon=True).start()

        # 计算待补全数量用于提示消息
        try:
            _tmp_series = get_all_series()
            _msg_count = len(_collect_uncached_videos(_tmp_series))
        except Exception:
            _msg_count = 0
        return JSONResponse({"ok": True, "message": f"已启动，共 {_msg_count} 个待补全"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})

def _clear_series_cache_file():
    """清空缓存文件"""
    try:
        if os.path.exists(SERIES_CACHE_FILE):
            pb_utils.write_series_cache(SERIES_CACHE_FILE, {})
            logger.info(f"[缓存] 已清空缓存文件")
    except Exception as e:
        logger.info(f"[缓存] 清空缓存文件失败: {e}")
@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, keyword: str = Query("", description="搜索关键词")):  # pyright: ignore[reportCallInDefaultInitializer]
    """搜索合集"""
    all_series = await asyncio.get_event_loop().run_in_executor(None, get_all_series)
    kw = keyword.strip().lower()

    if kw:
        filtered = [s for s in all_series if kw in s['name'].lower()]
    else:
        filtered = all_series

    # 统计总集数
    total_episodes = sum(s['episode_count'] for s in filtered)

    return render_template("index.html",
                           request=request,
                           series_list=filtered,
                           grouped={},
                           categories=load_categories(),
                           total_episodes=total_episodes,
                           unassigned_series=[],
                           page_title=f"搜索：{keyword} - 我的影院")


@app.get("/detail/{series_path:path}", response_class=HTMLResponse)
async def series_detail(request: Request, series_path: str):
    """视频详情页（快速渲染，详细信息通过 API 异步加载）"""
    def _find_category_for_directory(dir_path):
        """根据目录路径查找其所属的分类信息"""
        norm_dir = os.path.normpath(dir_path)
        all_series = _series_list_cache
        if not all_series:
            all_series = get_all_series()
        for s in all_series:
            s_path = s.get('path', '')
            if os.path.normpath(s_path) == norm_dir:
                cat = s.get('category')
                if isinstance(cat, dict) and cat.get('id') != 'default':
                    return cat
        return None

    def _find_category_for_directory(dir_path):
        norm_dir = os.path.normpath(dir_path)
        all_series = _series_list_cache
        if not all_series:
            all_series = get_all_series()
        for s in all_series:
            s_path = s.get('path', '')
            s_norm = os.path.normpath(s_path)
            if s_norm == norm_dir:
                cat = s.get('category')
                if isinstance(cat, dict) and cat.get('id') != 'default':
                    return cat
            try:
                if os.path.normpath(s_path.replace('\\', '/')) == os.path.normpath(norm_dir.replace('\\', '/')):
                    cat = s.get('category')
                    if isinstance(cat, dict) and cat.get('id') != 'default':
                        return cat
            except:
                pass
        return None

    series = await asyncio.get_event_loop().run_in_executor(None, _require_series, series_path)
    if not series:
        series = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _find_series_in_cache('abs:' + series_path))
    if not series:
        try:
            test_path = unquote(series_path)
            if test_path.startswith('abs:'):
                test_path = test_path[4:]
            if os.path.isdir(test_path):
                dir_name = os.path.basename(test_path.rstrip(os.sep))
                videos = []
                for f in sorted(os.listdir(test_path), key=_natural_sort_key):
                    fpath = os.path.join(test_path, f)
                    if os.path.isfile(fpath) and os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS:
                        videos.append({"filename": f, "filepath": fpath})
                if videos:
                    real_cat = _find_category_for_directory(test_path)
                    series = {
                        "name": dir_name,
                        "path": series_path,
                        "episode_count": len(videos),
                        "videos": videos,
                        "cover": None,
                        "orientation": get_video_orientation(videos[0]['filepath']) if videos else 'portrait',
                        "category": real_cat or {"id": "default", "name": _get_uncategorized_label(), "color": "#9499A0"},
                        "_is_abs": True,
                        "_abs_path": test_path,
                    }
        except Exception as e:
            logger.info(f"[警告] 详情页路径处理失败: {e}")
    if not series:
        return render_template("404.html", request=request, page_title="未找到 - 我的影院")
    video_dir = os.path.dirname(series['videos'][0]['filepath']) if series.get('videos') else ''
    cover_url = None
    if not series.get('_is_abs'):
        for cname in ['cover.jpg', 'cover.png', 'folder.jpg', 'folder.png']:
            cp = os.path.join(video_dir, cname)
            if os.path.exists(cp):
                cover_url = f'/cover/abs/{quote(video_dir)}/{quote(cname)}'
                break
        if not cover_url:
            cover_url = series.get('cover')
    first_thumb = None
    for ext in ['.jpg', '.jpeg', '.png']:
        first_vid = series.get('videos', [{}])[0]
        first_vid_path = first_vid.get('filepath', '')
        first_vid_dir = os.path.dirname(first_vid_path)
        first_vid_name = os.path.splitext(os.path.basename(first_vid_path))[0]
        candidate = os.path.join(first_vid_dir, first_vid_name + '_thumb' + ext)
        if os.path.isfile(candidate):
            first_thumb = f'/cover/abs/{quote(first_vid_dir)}/{quote(os.path.basename(candidate))}'
            break
    if not cover_url and first_thumb:
        cover_url = first_thumb
    # 只传基本信息，不调用 ffprobe
    basic_videos = []
    for i, v in enumerate(series.get('videos') or []):
        vid_path = v['filepath']
        vid_dir = os.path.dirname(vid_path)
        vid_name_without_ext = os.path.splitext(os.path.basename(vid_path))[0]
        vid_thumb = None
        for ext in ['.jpg', '.jpeg', '.png']:
            candidate = os.path.join(vid_dir, vid_name_without_ext + '_thumb' + ext)
            if os.path.isfile(candidate):
                vid_thumb = f'/cover/abs/{quote(vid_dir)}/{quote(os.path.basename(candidate))}'
                break
        basic_videos.append({
            'index': i + 1,
            'filename': v['filename'],
            'thumbnail': vid_thumb or '',
        })
    orientation = series.get('orientation', 'portrait')
    return render_template("detail.html",
                           request=request,
                           series=series,
                           cover_url=cover_url,
                           basic_videos=basic_videos,
                           orientation=orientation,
                           page_title=f"{series['name']} - 详情 - 我的影院")


@app.get("/api/detail/{series_path:path}")
async def api_series_detail(_request: Request, series_path: str):
    """获取剧集详细信息（优先从内存缓存读取，未命中的用线程池并行获取）"""

    def _find_category_for_directory(dir_path):
        norm_dir = os.path.normpath(dir_path)
        all_series = _series_list_cache
        if not all_series:
            all_series = get_all_series()
        for s in all_series:
            s_path = s.get('path', '')
            s_norm = os.path.normpath(s_path)
            if s_norm == norm_dir:
                cat = s.get('category')
                if isinstance(cat, dict) and cat.get('id') != 'default':
                    return cat
            try:
                if os.path.normpath(s_path.replace('\\', '/')) == os.path.normpath(norm_dir.replace('\\', '/')):
                    cat = s.get('category')
                    if isinstance(cat, dict) and cat.get('id') != 'default':
                        return cat
            except:
                pass
        return None

    series = await asyncio.get_event_loop().run_in_executor(None, _require_series, series_path)
    if not series:
        series = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _find_series_in_cache('abs:' + series_path))
    if not series:
        try:
            test_path = unquote(series_path)
            if test_path.startswith('abs:'):
                test_path = test_path[4:]
            if os.path.isdir(test_path):
                dir_name = os.path.basename(test_path.rstrip(os.sep))
                videos = []
                for f in sorted(os.listdir(test_path), key=_natural_sort_key):
                    fpath = os.path.join(test_path, f)
                    if os.path.isfile(fpath) and os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS:
                        videos.append({"filename": f, "filepath": fpath})
                if videos:
                    real_cat = _find_category_for_directory(test_path)
                    series = {
                        "name": dir_name,
                        "path": series_path,
                        "episode_count": len(videos),
                        "videos": videos,
                        "cover": None,
                        "orientation": get_video_orientation(videos[0]['filepath']) if videos else 'portrait',
                        "category": real_cat or {"id": "default", "name": _get_uncategorized_label(), "color": "#9499A0"},
                        "_is_abs": True,
                        "_abs_path": test_path,
                    }
        except Exception as e:
            logger.info(f"[警告] API详情页路径处理失败: {e}")
    if not series:
        return JSONResponse({"error": "Not found"}, status_code=404)

    video_list = series.get('videos') or []

    # 快速路径：先尝试从缓存批量读取，避免逐个 stat
    results = []
    uncached_indices = []  # (index, filepath) 需要实际获取的
    for i, v in enumerate(video_list):
        fp = v['filepath']
        cached = _get_meta_cached(fp)
        if cached and cached.get('resolution') and cached.get('resolution') != '未知':
            dur = cached.get('duration')
            results.append({
                'index': i + 1,
                'filename': v['filename'],
                'resolution': cached.get('resolution', '未知'),
                'duration_str': format_duration(dur) if dur and dur > 0 else '未知',
                'file_size_str': format_file_size(cached.get('file_size', 0)),
                'created_time': cached.get('created_time'),
                '_size': cached.get('file_size', 0),
            })
        else:
            uncached_indices.append((i, fp))

    # 未命中的视频用线程池并行获取
    if uncached_indices:
        def _fetch_uncached(args_list):
            out = []
            for idx, fp in args_list:
                info = get_video_info(fp)
                dur = get_video_duration(fp)
                out.append((idx, info, dur))
            return out

        from concurrent.futures import ThreadPoolExecutor
        fetched = await asyncio.get_event_loop().run_in_executor(
            ThreadPoolExecutor(max_workers=get_config_value("detail_workers", 8)),
            lambda: _fetch_uncached(uncached_indices)
        )
        for idx, info, dur in fetched:
            results.append({
                'index': idx + 1,
                'filename': video_list[idx]['filename'],
                'resolution': info.get('resolution', '未知'),
                'duration_str': format_duration(dur) if dur else '未知',
                'file_size_str': info.get('file_size_str', '0 B'),
                'created_time': info.get('created_time'),
                '_size': int(info.get('file_size', 0) or 0),
            })

    total_size = sum(int(r.pop('_size', 0)) for r in results)
    results.sort(key=lambda r: r['index'])

    # 后台保存缓存（不阻塞响应）
    if uncached_indices:
        threading.Thread(target=_save_video_meta_cache, daemon=True).start()

    return JSONResponse({
        'total_size_str': format_file_size(total_size),
        'videos': results,
    })


def _quick_video_info(video_path):
    """快速获取文件大小和创建时间（不调用 ffprobe，用于服务端渲染）"""
    info = {"resolution": "", "file_size_str": "", "created_time": ""}
    try:
        # 先尝试缓存
        cached = _get_meta_cached(video_path)
        if cached:
            info['resolution'] = cached.get('resolution') or ''
            fs = cached.get('file_size')
            if fs:
                info['file_size_str'] = format_file_size(fs)
            info['created_time'] = cached.get('created_time') or ''
        # 无论缓存是否命中，对缺失字段用 os.stat 补全
        if not info['file_size_str'] or not info['created_time']:
            stat = os.stat(video_path)
            if not info['file_size_str']:
                info['file_size_str'] = format_file_size(stat.st_size)
            if not info['created_time']:
                try:
                    info['created_time'] = time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_ctime))
                except (OSError, ValueError):
                    pass
    except Exception:
        pass
    return info


@app.get("/play/{series_path:path}/{episode_index:int}", response_class=HTMLResponse)
async def play_episode(request: Request, series_path: str, episode_index: int):
    """播放指定剧集（快速渲染，详细信息通过 AJAX 异步加载）"""
    assert series_path is not None, "series_path cannot be None"
    assert episode_index > 0, f"episode_index must be positive, got {episode_index}"

    def _find_category_for_file(filepath_str):
        """根据视频文件路径查找其所属的分类信息"""
        assert filepath_str is not None, "filepath_str cannot be None"
        
        norm_fp = os.path.normpath(filepath_str)
        all_series = _series_list_cache
        if not all_series:
            all_series = get_all_series()
        for s in all_series:
            for v in s.get('videos', []):
                if os.path.normpath(v.get('filepath', '')) == norm_fp:
                    cat = s.get('category')
                    if isinstance(cat, dict) and cat.get('id') != 'default':
                        return cat
                    return None
        return None

    def _find_category_for_directory(dir_path):
        norm_dir = os.path.normpath(dir_path)
        all_series = _series_list_cache
        if not all_series:
            all_series = get_all_series()
        for s in all_series:
            s_path = s.get('path', '')
            s_norm = os.path.normpath(s_path)
            if s_norm == norm_dir:
                cat = s.get('category')
                if isinstance(cat, dict) and cat.get('id') != 'default':
                    return cat
            try:
                if os.path.normpath(s_path.replace('\\', '/')) == os.path.normpath(norm_dir.replace('\\', '/')):
                    cat = s.get('category')
                    if isinstance(cat, dict) and cat.get('id') != 'default':
                        return cat
            except:
                pass
        return None

    series = await asyncio.get_event_loop().run_in_executor(None, _require_series, series_path)

    if not series or episode_index < 1 or episode_index > len(series['videos']):
        if '/' in series_path and episode_index >= 1:
            try:
                test_path = unquote(series_path)
                if test_path.startswith('abs:'):
                    test_path = test_path[4:]
                if os.path.isfile(test_path) and os.path.splitext(test_path)[1].lower() in ALLOWED_EXTENSIONS:
                    parent_dir = os.path.dirname(test_path)
                    filename = os.path.basename(test_path)
                    flat_videos = []
                    if os.path.isdir(parent_dir):
                        for f in sorted(os.listdir(parent_dir), key=_natural_sort_key):
                            fpath = os.path.join(parent_dir, f)
                            if os.path.isfile(fpath) and os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS:
                                flat_videos.append({"filename": f, "filepath": fpath})
                    if flat_videos and 1 <= episode_index <= len(flat_videos):
                        current_video = flat_videos[episode_index - 1]
                        base_path = series_path.rsplit('/', 1)[0]
                        real_cat = _find_category_for_file(current_video['filepath'])
                        series = {
                            "name": os.path.splitext(filename)[0],
                            "path": series_path,
                            "total_episodes": 1,
                            "episode_count": len(flat_videos),
                            "videos": flat_videos,
                            "cover": None,
                            "orientation": get_video_orientation(current_video['filepath']),
                            "category": real_cat or {"id": "default", "name": _get_uncategorized_label(), "color": "#9499A0"},
                            "_is_abs": True,
                            "_abs_path": parent_dir,
                            "_flat_base_path": base_path,
                        }
                        current_video = flat_videos[episode_index - 1]
                elif os.path.isdir(test_path) and episode_index >= 1:
                    dir_name = os.path.basename(test_path.rstrip(os.sep))
                    flat_videos = []
                    if os.path.isdir(test_path):
                        for f in sorted(os.listdir(test_path), key=_natural_sort_key):
                            fpath = os.path.join(test_path, f)
                            if os.path.isfile(fpath) and os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS:
                                flat_videos.append({"filename": f, "filepath": fpath})
                    if flat_videos and 1 <= episode_index <= len(flat_videos):
                        current_video = flat_videos[episode_index - 1]
                        real_cat = _find_category_for_directory(test_path)
                        series = {
                            "name": dir_name,
                            "path": series_path,
                            "total_episodes": 1,
                            "episode_count": len(flat_videos),
                            "videos": flat_videos,
                            "cover": None,
                            "orientation": get_video_orientation(current_video['filepath']),
                            "category": real_cat or {"id": "default", "name": _get_uncategorized_label(), "color": "#9499A0"},
                            "_is_abs": True,
                            "_abs_path": test_path,
                        }
                        current_video = flat_videos[episode_index - 1]
            except Exception as e:
                logger.info(f"[警告] 播放页面路径处理失败: {e}")
        if not series or episode_index < 1:
            resp = render_template('404.html',
                                  request=request,
                                  page_title="页面走丢啦")
            return Response(content=resp.body, status_code=404, media_type="text/html")

    videos_raw = series.get('videos', [])
    videos_list: list = videos_raw if isinstance(videos_raw, list) else []
    if episode_index > len(videos_list):
        resp = render_template('404.html',
                              request=request,
                              page_title="页面走丢啦")
        return Response(content=resp.body, status_code=404, media_type="text/html")

    current_video: dict = videos_list[episode_index - 1]  # type: ignore[index]

    if series.get('_flat_base_path') and int(series.get('episode_count', 0) or 0) > 1:
        pass
    elif int(series.get('episode_count', 0) or 0) == 1 and '/' in series_path:
        first_video: dict = videos_list[0]  # type: ignore[index]
        parent_dir = os.path.dirname(first_video['filepath'])
        if os.path.isdir(parent_dir):
            flat_videos = []
            for f in sorted(os.listdir(parent_dir), key=_natural_sort_key):
                fpath = os.path.join(parent_dir, f)
                if os.path.isfile(fpath) and os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS:
                    flat_videos.append({"filename": f, "filepath": fpath})
            if len(flat_videos) > 1:
                current_idx = next(
                    (i for i, v in enumerate(flat_videos) if v['filename'] == current_video["filename"]),
                    0
                )
                base_path = series_path.rsplit('/', 1)[0]
                expanded = dict(series)
                expanded['videos'] = flat_videos
                expanded['episode_count'] = len(flat_videos)
                expanded['_flat_base_path'] = base_path
                series = expanded
                episode_index = current_idx + 1
                current_video = flat_videos[current_idx]

    # === 播放列表合并逻辑 ===
    # 优先级（从高到低）：
    # 1. 合集容器内的子目录 → 不合并，只显示当前子目录的集数
    # 2. 混合目录下的视频 → 合并该混合目录下所有视频（直连 + 子目录中）
    # 3. 默认分类 → 不合并
    # 4. 其它分类 → 同分类合并

    def _check_dir_type(dir_path):
        """检查目录类型，返回 (has_direct_videos, has_video_subdirs)"""
        has_videos = False
        has_video_subdirs = False
        try:
            for item in os.listdir(dir_path):
                fpath = os.path.join(dir_path, item)
                if os.path.isfile(fpath) and not item.startswith('.'):
                    ext = os.path.splitext(item)[1].lower()
                    if ext in ALLOWED_EXTENSIONS:
                        has_videos = True
                elif os.path.isdir(fpath) and not item.startswith('.'):
                    if not has_video_subdirs:
                        try:
                            for sub_f in os.listdir(fpath):
                                if os.path.splitext(sub_f)[1].lower() in ALLOWED_EXTENSIONS:
                                    has_video_subdirs = True
                                    break
                        except OSError:
                            pass
        except OSError:
            pass
        return has_videos, has_video_subdirs

    def _collect_mixed_dir_videos(mixed_dir_path):
        """收集混合目录下所有视频（直连 + 子目录中的）"""
        all_series = get_all_series()
        mixed_norm = os.path.normpath(mixed_dir_path).rstrip(os.sep)
        merged = []
        seen_fps = set()
        for s in all_series:
            s_videos = s.get('videos', [])
            if not isinstance(s_videos, list) or not s_videos:
                continue
            s_name = s.get('name', '')
            s_path = os.path.normpath(s.get('path', '')).rstrip(os.sep)
            s_is_flat = (s.get('episode_count', 0) <= 1 or s.get('_flat_base_path'))
            for vi, v in enumerate(s_videos):
                v_fp = os.path.normpath(v.get('filepath', ''))
                if not v_fp or v_fp in seen_fps:
                    continue
                # 检查视频是否在该混合目录下
                if not v_fp.startswith(mixed_norm + os.sep):
                    continue
                seen_fps.add(v_fp)
                entry = dict(v)
                entry['_dir_name'] = s_name
                local_ep = vi + 1
                entry['_local_ep'] = local_ep
                if s_is_flat:
                    parent = os.path.dirname(v['filepath'])
                    parent_encoded = quote(parent.replace('\\', '/'))
                    entry['_play_path'] = parent_encoded + '/' + quote(v['filename'])
                    entry['_play_href'] = '/play/' + parent_encoded + '/' + quote(v['filename']) + '/' + str(local_ep)
                    entry['_video_url'] = '/video/' + parent_encoded + '/' + quote(v['filename'])
                else:
                    entry['_play_path'] = quote(s_path.replace('\\', '/')) + '/' + str(local_ep)
                    entry['_play_href'] = '/play/' + quote(s_path.replace('\\', '/')) + '/' + str(local_ep)
                    entry['_video_url'] = '/video/' + quote(s_path.replace('\\', '/'))
                    if not s_path.endswith('/' + v['filename']):
                        entry['_video_url'] += '/' + quote(v['filename'])
                merged.append(entry)
        return merged

    def _merge_category_videos(series_obj):
        """同分类合并（非混合目录、非合集容器的 series）"""
        cat_id = None
        cat = series_obj.get('category')
        if isinstance(cat, dict):
            cat_id = cat.get('id')
        if not cat_id or cat_id == 'default':
            return []
        all_series = get_all_series()
        merged = []
        seen_fps = set()
        for s in all_series:
            s_cat = s.get('category')
            s_cat_id = s_cat.get('id') if isinstance(s_cat, dict) else None
            if s_cat_id != cat_id:
                continue
            s_path = os.path.normpath(s.get('path', '')).rstrip(os.sep)
            s_videos = s.get('videos', [])
            if not isinstance(s_videos, list) or not s_videos:
                continue
            s_name = s.get('name', os.path.basename(s_path))
            s_is_flat = (s.get('episode_count', 0) <= 1 or s.get('_flat_base_path'))
            for vi, v in enumerate(s_videos):
                v_fp = os.path.normpath(v.get('filepath', ''))
                if not v_fp or v_fp in seen_fps:
                    continue
                seen_fps.add(v_fp)
                entry = dict(v)
                entry['_dir_name'] = s_name
                local_ep = vi + 1
                entry['_local_ep'] = local_ep
                if s_is_flat:
                    parent = os.path.dirname(v['filepath'])
                    parent_encoded = quote(parent.replace('\\', '/'))
                    entry['_play_path'] = parent_encoded + '/' + quote(v['filename'])
                    entry['_play_href'] = '/play/' + parent_encoded + '/' + quote(v['filename']) + '/' + str(local_ep)
                    entry['_video_url'] = '/video/' + parent_encoded + '/' + quote(v['filename'])
                else:
                    entry['_play_path'] = quote(s_path.replace('\\', '/')) + '/' + str(local_ep)
                    entry['_play_href'] = '/play/' + quote(s_path.replace('\\', '/')) + '/' + str(local_ep)
                    entry['_video_url'] = '/video/' + quote(s_path.replace('\\', '/'))
                    if not s_path.endswith('/' + v['filename']):
                        entry['_video_url'] += '/' + quote(v['filename'])
                merged.append(entry)
        return merged

    # 确定合并策略
    all_merged_videos = []
    videos_in_series = series.get('videos', [])
    if videos_in_series:
        first_fp = videos_in_series[0].get('filepath', '')
        video_dir = os.path.dirname(first_fp)
        in_collection = False
        mixed_dir = None

        # 先检查 video_dir 自身是否是混合目录
        dv, ds = _check_dir_type(video_dir)
        if dv and ds:
            mixed_dir = video_dir
        else:
            # 向上遍历祖先目录，查找最近的混合目录
            # 合集容器不阻止继续向上查找（混合目录下的合集也应合并）
            check_dir = video_dir
            while True:
                parent = os.path.dirname(check_dir)
                if not parent or parent == check_dir or not os.path.isdir(parent):
                    break
                pv, ps = _check_dir_type(parent)
                # 混合目录：记录为合并范围并停止
                if pv and ps:
                    mixed_dir = parent
                    break
                # 合集容器：记录但继续向上查找
                if not pv and ps:
                    in_collection = True
                check_dir = parent

        if in_collection and not mixed_dir:
            pass  # 纯合集容器内（不在混合目录下），不合并
        elif mixed_dir:
            all_merged_videos = _collect_mixed_dir_videos(mixed_dir)
        else:
            all_merged_videos = _merge_category_videos(series)

    if all_merged_videos:
        current_fp = os.path.normpath(current_video['filepath'])
        new_idx = next(
            (i for i, v in enumerate(all_merged_videos)
             if os.path.normpath(v.get('filepath', '')) == current_fp),
            episode_index - 1
        )
        series = dict(series)
        series['videos'] = all_merged_videos
        series['episode_count'] = len(all_merged_videos)
        series['_category_merged'] = True
        episode_index = new_idx + 1
        current_video = all_merged_videos[new_idx]

    # 优先使用合并视频预计算的 _video_url（合并后 series_path 可能与视频实际路径不匹配）
    if current_video.get('_video_url'):
        video_url = current_video['_video_url']
    else:
        safe_path = quote(series_path.replace('\\', '/'))
        if series_path.endswith('/' + current_video["filename"]):
            video_url = f'/video/{safe_path}'
        else:
            video_url = f'/video/{safe_path}/{quote(current_video["filename"])}'

    # 优先使用扫描时缓存的方向，避免重复打开视频文件
    orientation = series.get('orientation') or get_video_orientation(current_video['filepath'])

    # 快速渲染：当前集生成缩略图并嵌入 videos（线程池执行，不阻塞事件循环）
    current_idx = episode_index - 1
    videos = series.get('videos', [])
    if isinstance(videos, list) and current_idx < len(videos):
        def _prepare_thumb():
            generate_thumbnail(current_video['filepath'], verbose=False)
            # 优先使用视频自带的 thumbnail（合并播放列表中路径更准确）
            if current_video.get('thumbnail'):
                return current_video['thumbnail']
            return get_thumbnail_url(series_path, current_video['filename'])
        thumb_url = await asyncio.to_thread(_prepare_thumb)
        # 不可直接修改 series['videos'] 列表中的字典（可能影响缓存），创建副本
        videos_copy = [dict(v) for v in videos]
        videos_copy[current_idx]['thumbnail'] = thumb_url
        series = dict(series)
        series['videos'] = videos_copy

    return render_template("video.html",
                           request=request,
                           series=series,
                           current_video=current_video,
                           current_episode=episode_index,
                           video_url=video_url,
                           video_duration=None,
                           video_info=_quick_video_info(current_video['filepath']),
                           orientation=orientation,
                           subtitles=[],                           page_title=f"{series['name']} 第{os.path.splitext(current_video['filename'])[0]}集 - 热播{(series['category'] if isinstance(series['category'], dict) else {}).get('name', '合集')}")



@app.get("/api/play-info/{series_path:path}/{episode_index:int}")
async def api_play_info(series_path: str, episode_index: int):
    """异步返回播放页详细信息：当前视频信息、所有缩略图、字幕列表（不触发目录扫描）"""
    # 复用 play_episode 的系列查找逻辑
    series = await asyncio.get_event_loop().run_in_executor(None, _require_series, series_path)
    videos = []

    if series and 1 <= episode_index <= len(series.get('videos', [])):
        videos = series['videos']
        series_name = series.get('name', '')
        video_dir = os.path.dirname(videos[0]['filepath']) if videos else ''
    else:
        # fallback: 尝试直接解析路径（兼容 abs: 前缀等）
        decoded_path = unquote(series_path)
        video_dir = ""
        series_name = ""
        if series_path.startswith('abs:'):
            decoded_path = decoded_path[4:]
        if os.path.isdir(decoded_path):
            video_dir = decoded_path
            series_name = os.path.basename(decoded_path)
            for f in sorted(os.listdir(decoded_path), key=_natural_sort_key):
                fpath = os.path.join(decoded_path, f)
                if os.path.isfile(fpath) and os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS:
                    videos.append({"filename": f, "filepath": fpath})
        elif os.path.isfile(decoded_path) and os.path.splitext(decoded_path)[1].lower() in ALLOWED_EXTENSIONS:
            parent_dir = os.path.dirname(decoded_path)
            video_dir = parent_dir
            series_name = os.path.splitext(os.path.basename(decoded_path))[0]
            for f in sorted(os.listdir(parent_dir), key=_natural_sort_key):
                fpath = os.path.join(parent_dir, f)
                if os.path.isfile(fpath) and os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS:
                    videos.append({"filename": f, "filepath": fpath})

    if not videos or episode_index < 1 or episode_index > len(videos):
        return JSONResponse({"error": "not found"}, status_code=404)

    current_video = videos[episode_index - 1]
    
    # 在终端显示当前正在播放的视频信息
    logger.info(f"[播放] {series_name or '未知合集'} - 第{episode_index}集: {current_video['filename']}")

    # 构建 series 信息供后续使用
    if not series:
        series = {
            "name": series_name,
            "path": series_path,
            "episode_count": len(videos),
            "videos": videos,
            "_is_abs": series_path.startswith('abs:'),
        }
    if not video_dir:
        video_dir = os.path.dirname(current_video['filepath'])

    # 优先使用合并视频预计算的 _video_url
    if current_video.get('_video_url'):
        video_url = current_video['_video_url']
    else:
        safe_path = quote(series_path.replace('\\', '/'))
        if series_path.endswith('/' + current_video["filename"]):
            video_url = f'/video/{safe_path}'
        else:
            video_url = f'/video/{safe_path}/{quote(current_video["filename"])}'

    # 所有 I/O 操作打包到一个同步函数中，通过线程池执行，避免阻塞事件循环
    def _collect_play_data():
        info = get_video_info(current_video['filepath'])
        duration = get_video_duration(current_video['filepath'])
        # 当前集生成缩略图
        generate_thumbnail(current_video['filepath'], verbose=False)
        thumbs = []
        for i, v in enumerate(videos):
            # 优先使用视频自带的 thumbnail（合并播放列表中来自不同目录的视频路径正确）
            if v.get('thumbnail'):
                thumb_url = v['thumbnail']
            else:
                thumb_url = get_thumbnail_url(series_path, v['filename'])
            thumbs.append({"index": i + 1, "fp": v.get('filepath', ''), "thumbnail": thumb_url})
        # 合集封面
        cover = None
        if len(videos) > 1 and not series.get('_is_abs'):
            generate_series_cover(series, _verbose=False)
            cover_path = os.path.join(video_dir, 'cover.jpg')
            if os.path.exists(cover_path):
                if series.get('_is_abs'):
                    cover = f'/cover/abs/{quote(video_dir)}/cover.jpg'
                else:
                    cover = f'/cover/{quote(series_path)}/cover.jpg'
        subs = find_subtitles(current_video['filepath'], series_path, episode_index)
        return info, duration, subs, thumbs, cover

    video_info, video_duration, subtitles, thumbnails, cover_url = await asyncio.to_thread(_collect_play_data)

    return JSONResponse({
        "video_url": video_url,
        "video_info": video_info,
        "video_duration": video_duration,
        "subtitles": subtitles,
        "thumbnails": thumbnails,
        "cover": cover_url,
    })


@app.get("/subtitle/{series_path:path}/{episode_index:int}/{filename:path}")
async def serve_subtitle(series_path: str, episode_index: int, filename: str):
    """提供字幕文件下载（直接从路径推导视频目录，无需全量扫描）"""
    del episode_index  # URL 路径参数，保留用于路由匹配但不使用
    filename = unquote(filename)
    # 从 series_path 和 episode_index 推导视频所在目录
    decoded_path = unquote(series_path)
    if os.path.isfile(decoded_path):
        video_dir = os.path.dirname(decoded_path)
    elif os.path.isdir(decoded_path):
        video_dir = decoded_path
    else:
        # 可能是相对路径，尝试拼接 VIDEO_BASE_DIR
        candidate = decoded_path
        if '/' in decoded_path:
            parts = decoded_path.rsplit('/', 1)
            candidate = parts[0]
        candidate = candidate.replace('/', os.sep)
        full = candidate if os.path.isabs(candidate) else os.path.join(VIDEO_BASE_DIR, candidate)
        video_dir = full if os.path.isdir(full) else os.path.dirname(full)
    subtitle_path = os.path.join(video_dir, filename)
    if not _is_abs_path_allowed(subtitle_path):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not os.path.exists(subtitle_path) or not os.path.isfile(subtitle_path):
        return JSONResponse({"error": "Subtitle not found"}, status_code=404)
    ext = os.path.splitext(filename)[1].lower()
    media_type = SUBTITLE_MIME_MAP.get(ext, 'text/plain')
    return FileResponse(path=subtitle_path, media_type=media_type)


@app.head("/video/{filepath:path}")
async def serve_video_head(filepath: str):
    filepath = unquote(filepath)
    if re.match(r'^[a-zA-Z]:', filepath) or os.path.isabs(filepath):
        full_path = filepath.replace('/', os.sep)
        if not _is_abs_path_allowed(full_path):
            return Response(status_code=403)
    else:
        full_path = os.path.join(VIDEO_BASE_DIR, filepath)
        if not _is_path_safe(VIDEO_BASE_DIR, full_path):
            return Response(status_code=403)

    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        ext = os.path.splitext(full_path)[1].lower()

        # 情况1：路径本身是需转码格式 → 尝试预转码文件
        if ext in TRANSCODE_FORMATS:
            encoded_path = _get_encoded_path(full_path)
            if os.path.exists(encoded_path):
                file_size = os.path.getsize(encoded_path)
                return Response(
                    status_code=200,
                    headers={
                        'Content-Type': 'video/mp4',
                        'Content-Length': str(file_size),
                        'Accept-Ranges': 'bytes',
                    }
                )

        # 情况2：路径是 .mp4 但不存在 → 尝试同名原始格式
        if ext == '.mp4':
            base = os.path.splitext(full_path)[0]
            for alt_ext in TRANSCODE_FORMATS:
                alt_path = base + alt_ext
                if os.path.exists(alt_path):
                    encoded = _get_encoded_path(alt_path)
                    if os.path.exists(encoded):
                        return Response(status_code=200, headers={
                            'Content-Type': 'video/mp4',
                            'Content-Length': str(os.path.getsize(encoded)),
                            'Accept-Ranges': 'bytes',
                        })
                    break

        # 情况3：URL 中"目录名"是已删除的原格式文件名（同 GET 处理逻辑）
        dir_part = os.path.dirname(full_path)
        fname = os.path.basename(full_path)
        dir_name = os.path.basename(dir_part)
        dir_ext = os.path.splitext(dir_name)[1].lower()
        if dir_ext and dir_ext in TRANSCODE_FORMATS:
            grandparent = os.path.dirname(dir_part)
            candidate = os.path.join(grandparent, fname) if grandparent else None
            if candidate and os.path.isfile(candidate):
                c_ext = os.path.splitext(candidate)[1].lower()
                if c_ext == '.mp4':
                    return Response(status_code=200, headers={
                        'Content-Type': 'video/mp4',
                        'Content-Length': str(os.path.getsize(candidate)),
                        'Accept-Ranges': 'bytes',
                    })
                enc_cand = _get_encoded_path(candidate) if c_ext in TRANSCODE_FORMATS else None
                if enc_cand and os.path.exists(enc_cand):
                    return Response(status_code=200, headers={
                        'Content-Type': 'video/mp4',
                        'Content-Length': str(os.path.getsize(enc_cand)),
                        'Accept-Ranges': 'bytes',
                    })

        return Response(status_code=404)

    ext = os.path.splitext(full_path)[1].lower()
    mime_type = MIME_MAP.get(ext, 'video/mp4')
    file_size = os.path.getsize(full_path)
    return Response(
        status_code=200,
        headers={
            'Content-Type': mime_type,
            'Content-Length': str(file_size),
            'Accept-Ranges': 'bytes',
        }
    )

@app.get("/video/{filepath:path}")
async def serve_video(filepath: str, request: Request):
    """提供视频文件播放（支持Range请求/断点续传，不支持的格式自动转码）"""
    filepath = unquote(filepath)
    if re.match(r'^[a-zA-Z]:', filepath) or os.path.isabs(filepath):
        full_path = filepath.replace('/', os.sep)
        if not _is_abs_path_allowed(full_path):
            return Response(content="禁止访问", status_code=403)
    else:
        full_path = os.path.join(VIDEO_BASE_DIR, filepath)
        if not _is_path_safe(VIDEO_BASE_DIR, full_path):
            return Response(content="禁止访问", status_code=403)

    def _try_serve_file(target_path, label=''):
        """尝试提供目标文件（优先预转码 .mp4）"""
        if not target_path or not os.path.isfile(target_path):
            return None
        t_ext = os.path.splitext(target_path)[1].lower()
        if t_ext != '.mp4' and t_ext in TRANSCODE_FORMATS:
            enc = _get_encoded_path(target_path)
            if os.path.exists(enc):
                logger.info(f"[{label}] 使用预转码文件: {os.path.basename(enc)}")
                return FileResponse(path=enc, media_type='video/mp4',
                                    filename=os.path.basename(enc), stat_result=os.stat(enc))
        # 直接返回可播放文件
        mime = MIME_MAP.get(t_ext, 'video/mp4')
        return FileResponse(path=target_path, media_type=mime,
                            filename=os.path.basename(target_path), stat_result=os.stat(target_path))

    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        ext = os.path.splitext(full_path)[1].lower()

        # 情况1：路径本身是需转码格式 → 尝试预转码文件
        resp = _try_serve_file(_get_encoded_path(full_path), '已转码')
        if resp:
            return resp

        # 情况2：路径是 .mp4 但不存在 → 尝试同名原始格式
        if ext == '.mp4':
            base = os.path.splitext(full_path)[0]
            for alt_ext in TRANSCODE_FORMATS:
                alt_path = base + alt_ext
                if os.path.exists(alt_path):
                    resp = _try_serve_file(alt_path, '回退原文件')
                    if resp:
                        return resp
                    break

        # 情况3（关键！）：URL 中"目录名"是已删除的原格式文件名
        # 例如: E:\视频\xxx.webm\xxx.mp4 → 原始 xxx.webm 已删除，实际文件在 E:\视频\xxx.mp4
        dir_part = os.path.dirname(full_path)
        fname = os.path.basename(full_path)
        dir_name = os.path.basename(dir_part)
        dir_ext = os.path.splitext(dir_name)[1].lower()
        if dir_ext and dir_ext in TRANSCODE_FORMATS:
            # 构造去掉一层目录后的候选路径: 祖父目录/当前文件名
            grandparent = os.path.dirname(dir_part)
            candidate = os.path.join(grandparent, fname) if grandparent else None
            if candidate:
                # 安全校验：使用 realpath 标准化路径并验证
                candidate_real = os.path.realpath(candidate)
                if _is_path_safe(VIDEO_BASE_DIR, candidate_real) and os.path.isfile(candidate):
                    resp = _try_serve_file(candidate, '旧URL回退(去原格式层)')
                    if resp:
                        return resp
            # 也尝试用目录名(不含扩展名)+文件名的组合
            dir_stem = os.path.splitext(dir_name)[0]
            stem_mp4 = os.path.join(grandparent, dir_stem + '.mp4') if grandparent else None
            if stem_mp4 and stem_mp4 != full_path:
                # 安全校验：使用 realpath 标准化路径并验证
                stem_mp4_real = os.path.realpath(stem_mp4)
                if _is_path_safe(VIDEO_BASE_DIR, stem_mp4_real) and os.path.isfile(stem_mp4):
                    resp = _try_serve_file(stem_mp4, '旧URL回退(同名录)')
                    if resp:
                        return resp

        logger.info(f"[404] 视频不存在: {full_path}")
        return Response(content="视频文件不存在", status_code=404)

    ext = os.path.splitext(full_path)[1].lower()

    # 如果是需要转码的格式，先检查是否有已转码的文件
    if ext in TRANSCODE_FORMATS:
        encoded_path = _get_encoded_path(full_path)
        if os.path.exists(encoded_path):
            logger.info(f"[已转码] 使用预转码文件: {os.path.basename(encoded_path)}")
            return FileResponse(
                path=encoded_path,
                media_type='video/mp4',
                filename=os.path.basename(encoded_path),
                stat_result=os.stat(encoded_path),
            )
        if not check_ffmpeg():
            return Response(content=f"需要 FFmpeg 才能播放 .{ext} 格式，请安装 FFmpeg 后重试",
                           status_code=415, media_type='text/plain')

        range_header = request.headers.get('Range')
        start_time = 0
        if range_header:
            m = re.search(r'bytes=(\d+)-', range_header)
            if m:
                byte_offset = int(m.group(1))
                bitrate = get_video_bitrate(full_path)
                if bitrate and bitrate > 0:
                    start_time = byte_offset * 8 / bitrate
                else:
                    start_time = byte_offset // 1000
                logger.info(f"[转码] Range 请求: byte_offset={byte_offset} bitrate={bitrate} start_time={start_time:.2f}s")
        logger.info(f"[转码] 使用 FFmpeg 实时转码: {os.path.basename(full_path)}")
        safe_name = quote(os.path.basename(full_path))
        headers = {
            'Content-Disposition': f'inline; filename="{safe_name}"',
            'Accept-Ranges': 'bytes',
        }
        return StreamingResponse(
            stream_transcoded_video(full_path, start_time=start_time),
            media_type='video/mp4',
            headers=headers,
            status_code=200 if start_time == 0 else 200
        )

    # .mp4 文件编码兼容性检查（如 MPEG-4 Part 2 浏览器无法解码，需转码）
    if ext == '.mp4' and not _is_mp4_browser_playable(full_path):
        logger.info(f"[非标准MP4] {os.path.basename(full_path)} 编码不兼容浏览器，走转码路径")
        encoded_path = _get_encoded_path(full_path)
        if os.path.exists(encoded_path):
            logger.info(f"[已转码] 使用预转码文件: {os.path.basename(encoded_path)}")
            return FileResponse(
                path=encoded_path,
                media_type='video/mp4',
                filename=os.path.basename(encoded_path),
                stat_result=os.stat(encoded_path),
            )
        if not check_ffmpeg():
            return Response(content="该视频编码浏览器不支持且 FFmpeg 不可用",
                           status_code=415, media_type='text/plain')
        range_header = request.headers.get('Range')
        start_time = 0
        if range_header:
            m = re.search(r'bytes=(\d+)-', range_header)
            if m:
                byte_offset = int(m.group(1))
                bitrate = get_video_bitrate(full_path)
                start_time = (byte_offset * 8 / bitrate) if bitrate and bitrate > 0 else byte_offset // 1000
                logger.info(f"[转码] Range 请求: byte_offset={byte_offset} start_time={start_time:.2f}s")
        logger.info(f"[转码] 使用 FFmpeg 实时转码: {os.path.basename(full_path)}")
        safe_name = quote(os.path.basename(full_path))
        return StreamingResponse(
            stream_transcoded_video(full_path, start_time=start_time),
            media_type='video/mp4',
            headers={'Content-Disposition': f'inline; filename="{safe_name}"'},
            status_code=200,
        )

    mime_type = MIME_MAP.get(ext, 'application/octet-stream')
    file_size = os.path.getsize(full_path)
    range_header = request.headers.get('Range')
    
    # 生成 ETag
    etag = None
    if CDN_ETAG_ENABLED:
        stat = os.stat(full_path)
        etag = _generate_etag(full_path, stat.st_mtime, file_size)
    
    # 检查 If-None-Match（304 Not Modified）
    if CDN_ETAG_ENABLED and etag:
        client_etag = _parse_if_none_match(dict(request.headers))
        if client_etag and client_etag == etag.strip('"'):
            return Response(status_code=304, headers={
                'ETag': etag,
                'Cache-Control': f'public, max-age={CDN_CACHE_CONTROL_MAX_AGE}',
            })
    
    # CDN 缓存头
    cache_headers = {
        'Cache-Control': f'public, max-age={CDN_CACHE_CONTROL_MAX_AGE}, immutable, stale-while-revalidate=3600',
        'Accept-Ranges': 'bytes',
    }
    if etag:
        cache_headers['ETag'] = etag

    if range_header:
        m = re.search(r'bytes=(\d*)-(\d*)', range_header)
        if m:
            start_byte = int(m.group(1)) if m.group(1) else 0
            end_byte = int(m.group(2)) if m.group(2) else file_size - 1
            if start_byte >= file_size:
                return Response(
                    content="Requested Range Not Satisfiable",
                    status_code=416,
                    headers={**cache_headers, 'Content-Range': f'bytes */{file_size}'}
                )
            end_byte = min(end_byte, file_size - 1)
            content_length = end_byte - start_byte + 1

            # L1 缓存检查
            cache_key = _get_content_cache_key(full_path, start_byte, end_byte)
            cached_data = _cache_get_l1(full_path, start_byte, end_byte)
            if cached_data:
                logger.info(f"[缓存 L1] 命中: {os.path.basename(full_path)} [{start_byte}-{end_byte}]")
                return Response(
                    content=cached_data,
                    status_code=206,
                    media_type=mime_type,
                    headers={
                        **cache_headers,
                        'Content-Range': f'bytes {start_byte}-{end_byte}/{file_size}',
                        'Content-Length': str(len(cached_data)),
                        'Content-Disposition': f'inline; filename="{quote(os.path.basename(full_path))}"',
                    }
                )

            async def stream_range_async():
                def read_chunks():
                    with open(full_path, 'rb') as f:
                        f.seek(start_byte)
                        remaining = content_length
                        while remaining > 0:
                            chunk_size = min(1024 * 1024, remaining)
                            chunk = f.read(chunk_size)
                            if not chunk:
                                break
                            remaining -= len(chunk)
                            yield chunk
                for chunk in read_chunks():
                    await asyncio.sleep(0)
                    yield chunk

            return StreamingResponse(
                stream_range_async(),
                status_code=206,
                media_type=mime_type,
                headers={
                    **cache_headers,
                    'Content-Range': f'bytes {start_byte}-{end_byte}/{file_size}',
                    'Content-Length': str(content_length),
                    'Content-Disposition': f'inline; filename="{quote(os.path.basename(full_path))}"',
                }
            )

    # 完整文件请求：检查 L1 缓存
    cached_data = _cache_get_l1(full_path)
    if cached_data:
        logger.info(f"[缓存 L1] 命中: {os.path.basename(full_path)} (完整)")
        return Response(
            content=cached_data,
            status_code=200,
            media_type=mime_type,
            headers={
                **cache_headers,
                'Content-Length': str(len(cached_data)),
                'Content-Disposition': f'inline; filename="{quote(os.path.basename(full_path))}"',
            }
        )

    return FileResponse(
        path=full_path,
        media_type=mime_type,
        filename=os.path.basename(full_path),
        stat_result=os.stat(full_path),
        headers=cache_headers,
    )


@app.get("/cover/abs/{dir_path:path}/{filename}")
async def serve_cover_abs(dir_path: str, filename: str):
    """提供绝对路径目录的封面图片"""
    file_path = os.path.join(unquote(dir_path), filename)

    if not _is_abs_path_allowed(file_path):
        return Response(content="禁止访问", status_code=403)

    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return Response(content="文件不存在", status_code=404)

    ext = os.path.splitext(filename)[1].lower()
    mime_type = 'image/jpeg' if ext in ('.jpg', '.jpeg') else ('image/png' if ext == '.png' else 'image/jpeg')

    return FileResponse(
        path=file_path,
        media_type=mime_type,
        filename=filename,
        stat_result=os.stat(file_path),
    )


@app.get("/cover/{series_path}/{filename}")
async def serve_cover(series_path: str, filename: str):
    """提供剧集封面图片（原始目录中的图片）"""
    cover_dir = os.path.join(VIDEO_BASE_DIR, unquote(series_path))
    file_path = os.path.join(cover_dir, filename)

    if not _is_path_safe(VIDEO_BASE_DIR, file_path):
        return Response(content="禁止访问", status_code=403)

    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return Response(content="文件不存在", status_code=404)

    ext = os.path.splitext(filename)[1].lower()
    mime_type = 'image/jpeg' if ext in ('.jpg', '.jpeg') else ('image/png' if ext == '.png' else 'image/jpeg')

    return FileResponse(
        path=file_path,
        media_type=mime_type,
        filename=filename,
        stat_result=os.stat(file_path),
    )


@app.exception_handler(404)
async def page_not_found(request: Request, exc):  # exc 保留：FastAPI 异常处理器签名要求
    del exc
    """404错误处理"""
    resp = render_template('404.html',
                          request=request,
                          page_title="页面走丢啦 - 我的影院")
    return Response(content=resp.body, status_code=404, media_type="text/html")


# ====================== 启动配置 ======================
def _background_init():
    if not is_auto_scan_enabled():
        logger.info("[后台] 初始化完成！（自动扫描已关闭）")
        return
    logger.info("[后台] 开始扫描视频目录...")
    cleanup_stale_cache()
    # 后台执行扫描，不阻塞初始化
    def _scan_in_background():
        try:
            all_series = get_all_series()
            logger.info(f"[后台] 发现 {len(all_series)} 个合集系列")
        except Exception as e:
            logger.info(f"[后台] 扫描异常: {e}")
        start_file_watcher()
        logger.info("[后台] 初始化完成！")
        threading.Thread(target=_idle_thumbnail_checker, daemon=True).start()
        # 启动预加载工作线程
        _start_prefetch_worker()
    
    threading.Thread(target=_scan_in_background, daemon=True).start()


# 空闲检测子任务锁，防止并发执行同一任务
_idle_thumb_gen_lock = threading.Lock()
_idle_thumb_verify_lock = threading.Lock()
_idle_meta_lock = threading.Lock()
_idle_cleanup_lock = threading.Lock()
_idle_thumb_gen_last = 0  # 上次执行时间戳
_idle_thumb_verify_last = 0
_idle_meta_last = 0
_idle_cleanup_last = 0


def _idle_thumbnail_checker():
    """空闲时检测缩略图和封面尺寸，不正确的重新生成（重量级操作拆分到独立线程）"""
    while True:
        idle_interval = get_config_value("idle_check_interval", 300)
        time.sleep(idle_interval)
        if not is_auto_scan_enabled():
            continue
        # 转码进行中时跳过，避免磁盘 IO 冲突
        with _transcode_lock:
            transcode_active = bool(_transcode_in_progress)
        if transcode_active:
            continue

        logger.info("[后台] 开始空闲检测...")
        try:
            all_series = get_all_series()
        except Exception as e:
            logger.info(f"[后台] 获取系列列表异常: {e}")
            continue

        # 快速检查：哪些视频缺少缩略图、哪些需要转码（纯内存 + 文件存在检查，极快）
        missing_thumbs = []
        pending_transcode = []
        for series in all_series:
            for video in series['videos']:
                video_path = video.get('filepath')
                if not video_path:
                    continue
                video_name = os.path.splitext(os.path.basename(video_path))[0]
                thumb_name = video_name + '_thumb.jpg'
                thumb_path = os.path.join(os.path.dirname(video_path), thumb_name)
                if not os.path.exists(thumb_path):
                    if _video_needs_transcode(video_path):
                        pending_transcode.append(video_path)
                    else:
                        missing_thumbs.append(video_path)

        # 检测需要转码的视频，加入队列（仅在自动转码开启时）
        if pending_transcode and _config.get("auto_transcode", True):
            logger.info(f"[后台] 发现 {len(pending_transcode)} 个视频需要转码...")
            with _transcode_lock:
                for video_path in pending_transcode:
                    if video_path not in _transcode_queue and video_path not in _transcode_in_progress:
                        _transcode_queue.append(video_path)
            # _mark_video_pending_transcode 在锁外调用，避免嵌套锁
            for video_path in pending_transcode:
                _mark_video_pending_transcode(video_path, os.path.dirname(video_path))

        now = time.time()
        workers = get_config_value("meta_workers", 4)

        # 值拷贝：避免子线程引用被后续代码修改的变量
        _missing_thumbs_copy = list(missing_thumbs)
        _all_series_copy = list(all_series)
        _idle_interval_copy = idle_interval
        _workers_copy = workers
        _now_copy = now

        # 子任务1：生成缺失缩略图（独立线程，不阻塞主循环）
        def _run_thumb_gen():
            global _idle_thumb_gen_last
            if not _missing_thumbs_copy:
                return
            if _now_copy - _idle_thumb_gen_last < _idle_interval_copy:
                return  # 距离上次执行不足一个周期
            with _idle_thumb_gen_lock:
                if _now_copy - _idle_thumb_gen_last < _idle_interval_copy:
                    return
                _idle_thumb_gen_last = _now_copy
            logger.info(f"[后台] 生成 {len(_missing_thumbs_copy)} 个缺失缩略图...")
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _gen_one(fp):
                return fp, generate_thumbnail(fp, verbose=True)

            with ThreadPoolExecutor(max_workers=_workers_copy) as pool:
                futures = {pool.submit(_gen_one, fp): fp for fp in _missing_thumbs_copy}
                created = 0
                for future in as_completed(futures):
                    fp = futures[future]
                    try:
                        _, result = future.result()
                    except Exception:
                        result = None
                    if result == 'created':
                        created += 1
                        logger.info(f"[后台] ✓ 已生成缩略图: {os.path.basename(fp)}")
                    elif result is None:
                        logger.info(f"[后台] ✗ 缩略图生成失败: {os.path.basename(fp)}")
            if created > 0:
                save_thumbnail_cache()

        # 子任务2：验证并修复所有缩略图尺寸（独立线程）
        def _run_thumb_verify():
            global _idle_thumb_verify_last
            if _now_copy - _idle_thumb_verify_last < _idle_interval_copy:
                return
            with _idle_thumb_verify_lock:
                if _now_copy - _idle_thumb_verify_last < _idle_interval_copy:
                    return
                _idle_thumb_verify_last = _now_copy
            logger.info("[后台] 验证缩略图尺寸...")
            fixed = verify_and_regenerate_thumbnails(_all_series_copy, verbose=True)
            if fixed > 0:
                save_thumbnail_cache()
                logger.info(f"[后台] 缩略图尺寸修复: {fixed}个")
        # 子任务3：生成合集封面（独立线程）
        def _run_cover_gen():
            global _idle_cover_last
            if _now_copy - _idle_cover_last < _idle_interval_copy:
                return
            with _idle_thumb_gen_lock:
                pass  # cover 和 thumb gen 可以并发
            # covers 数量少，串行即可
            covers_fixed = generate_all_series_covers(_all_series_copy, verbose=True)
            if covers_fixed > 0:
                logger.info(f"[后台] 合集封面修复: {covers_fixed}个")
        # 子任务4：补全元数据缓存（独立线程，统一状态管理）
        def _run_meta_populate():
            global _idle_meta_last
            if _now_copy - _idle_meta_last < _idle_interval_copy:
                return
            with _idle_cleanup_lock:  # 复用锁做双重检查
                pass
            if _is_meta_populate_running():
                return
            if not _start_meta_populate():
                return
            try:
                populate_all_video_meta(series_list=_all_series_copy)
            finally:
                _idle_meta_last = time.time()

        # 子任务5：定期清理已删除目录/视频的缓存条目（每 5 个周期执行一次）
        def _run_cleanup():
            global _idle_cleanup_last
            if _now_copy - _idle_cleanup_last < _idle_interval_copy * 5:
                return
            with _idle_cleanup_lock:
                if _now_copy - _idle_cleanup_last < _idle_interval_copy * 5:
                    return
                _idle_cleanup_last = _now_copy
            logger.info("[后台] 定期清理过期缓存...")
            cleanup_stale_cache()

        # 统一在后台启动所有子任务（主循环不等待）
        for task_fn in [_run_thumb_gen, _run_thumb_verify, _run_cover_gen, _run_meta_populate, _run_cleanup]:
            t = threading.Thread(target=task_fn, daemon=True)
            t.start()

        # 记录封面任务上次执行时间
        global _idle_cover_last
        _idle_cover_last = now


_transcode_queue = []
_transcode_in_progress = {}
_transcode_progress = {}  # {video_path: float}  0~100 百分比
_transcode_lock = threading.Lock()
_transcode_worker_started = False
_server_start_time = None  # 服务器启动时间


def _detect_and_queue_transcode(new_dirs):
    """检测新目录中需要转码的视频，并加入转码队列"""
    if not new_dirs:
        return {"detected": 0, "queued": 0, "videos": []}

    if not _config.get("auto_transcode", True):
        return {"detected": 0, "queued": 0, "skip": "自动转码已关闭"}

    if not check_ffmpeg():
        return {"detected": 0, "queued": 0, "error": "FFmpeg 未安装"}

    pending_videos = []
    for base_dir in new_dirs:
        if not os.path.isdir(base_dir):
            continue
        for root, _, files in os.walk(base_dir):
            for filename in files:
                ext = os.path.splitext(filename)[1].lower()
                if ext in TRANSCODE_FORMATS:
                    video_path = os.path.join(root, filename)
                    encoded_path = _get_encoded_path(video_path)
                    if not os.path.exists(encoded_path):
                        pending_videos.append({
                            "path": video_path,
                            "name": filename,
                            "encoded": encoded_path
                        })

    if not pending_videos:
        return {"detected": 0, "queued": 0, "videos": []}

    global _transcode_worker_started
    with _transcode_lock:
        for video in pending_videos:
            if video["path"] not in _transcode_queue and video["path"] not in _transcode_in_progress:
                _transcode_queue.append(video["path"])
        queued_count = len(_transcode_queue)
        # Worker 由 _lifespan 统一启动，此处不再重复启动

    video_info = [{"name": v["name"], "path": v["path"]} for v in pending_videos[:10]]
    if len(pending_videos) > 10:
        video_info.append({"name": f"...还有 {len(pending_videos) - 10} 个", "path": ""})

    logger.info(f"[预转码] 检测到 {len(pending_videos)} 个视频待转码，已加入队列")
    return {
        "detected": len(pending_videos),
        "queued": queued_count,
        "videos": video_info
    }

def _get_encoded_path(original_path):
    """获取已转码文件的路径（保持原名，只改扩展名为.mp4）"""
    directory = os.path.dirname(original_path)
    basename = os.path.basename(original_path)
    name, _ = os.path.splitext(basename)
    return os.path.join(directory, f"{name}.mp4")

def _get_all_video_paths():
    """遍历所有系列，获取需要转码的视频路径"""
    paths = []
    try:
        all_series = get_all_series()
        for series in all_series:
            for video in series.get('videos', []):
                video_path = video.get('filepath')
                if not video_path:
                    continue
                ext = os.path.splitext(video_path)[1].lower()
                if ext in TRANSCODE_FORMATS:
                    paths.append(video_path)
    except Exception as e:
        logger.info(f"[预转码] 获取视频列表异常: {e}")
    return paths

def _get_ffprobe_duration(video_path):
    """用 ffprobe 获取视频时长，失败返回 None"""
    ffprobe_cmd = os.path.join(BASE_DIR, 'ffmpeg', 'ffprobe.exe')
    if not os.path.exists(ffprobe_cmd):
        ffprobe_cmd = 'ffprobe'
    try:
        result = subprocess.run(
            [ffprobe_cmd, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', video_path],
            capture_output=True, timeout=15
        )
        if result.returncode == 0:
            dur = float(result.stdout.decode().strip())
            return dur if dur > 0 else None
    except Exception:
        pass
    return None


def _transcode_video_file(video_path):
    """转码单个视频文件，实时更新进度到 _transcode_progress"""
    encoded_path = _get_encoded_path(video_path)
    if os.path.exists(encoded_path):
        return True
    ffmpeg_cmd = get_ffmpeg_cmd()
    use_gpu = is_gpu_transcode_enabled()
    if use_gpu:
        # NVIDIA NVENC 硬件加速转码
        cmd = [
            ffmpeg_cmd,
            '-i', video_path,
            '-vcodec', 'h264_nvenc',
            '-acodec', 'aac',
            '-preset', 'p4',           # NVENC 预设：p1(最快)~p7(最慢最好)
            '-cq', '23',               # 恒定质量模式（NVENC 用 -cq 替代 CRF）
            '-b:v', '0',              # 配合 -cq 使用
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            '-progress', 'pipe:1',
            '-y',
            encoded_path
        ]
        gpu_label = " [GPU-NVENC]"
    else:
        # CPU 软编码（libx264）
        cmd = [
            ffmpeg_cmd,
            '-i', video_path,
            '-vcodec', 'libx264',
            '-acodec', 'aac',
            '-preset', 'medium',
            '-crf', '23',
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            '-progress', 'pipe:1',
            '-y',
            encoded_path
        ]
        gpu_label = ""
    logger.info(f"[预转码] ▶ 开始: {os.path.basename(video_path)}{gpu_label}")
    total_dur = get_video_duration(video_path)
    if not total_dur or total_dur <= 0:
        total_dur = _get_ffprobe_duration(video_path) or 0
    logger.info(f"[预转码]   时长: {total_dur:.1f}s")
    with _transcode_lock:
        _transcode_progress[video_path] = 0.0
    # 记录原始文件大小用于无时长时的进度估算
    src_size = os.path.getsize(video_path) if os.path.exists(video_path) else 0
    last_pct = 0.0
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        stdout = proc.stdout
        assert stdout is not None
        # 用 reader 线程避免 readline 阻塞
        import queue as _queue
        _line_queue: _queue.Queue = _queue.Queue()
        def _reader():
            try:
                for raw_line in stdout:
                    _line_queue.put(raw_line)
            finally:
                _line_queue.put(None)  # 哨兵值表示 EOF
        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()
        last_active_time = time.time()
        while True:
            try:
                line = _line_queue.get(timeout=5)
            except _queue.Empty:
                # 超时检查：进程是否已结束
                if proc.poll() is not None:
                    break
                # 如果超过 120 秒没有新输出，认为进程已卡死
                if time.time() - last_active_time > 120:
                    logger.info(f"[预转码] ✗ 进程无响应: {os.path.basename(video_path)}")
                    proc.kill()
                    proc.wait(timeout=10)
                    break
                # 超时无新进度行时，也尝试用文件大小更新进度（对 NVENC 尤其重要）
                if src_size > 0 and os.path.exists(encoded_path):
                    size_pct = min(99.0, (os.path.getsize(encoded_path) / src_size) * 100)
                    if size_pct - last_pct >= 2:
                        last_pct = size_pct
                        with _transcode_lock:
                            _transcode_progress[video_path] = size_pct
                continue
            if line is None:
                break
            last_active_time = time.time()
            line_str = line.decode('utf-8', errors='ignore').strip()
            if line_str.startswith('out_time_us='):
                try:
                    time_us = int(line_str.split('=')[1])
                    time_s = time_us / 1_000_000
                    # 方法1: 基于 out_time_us 的时间进度
                    time_pct = 0.0
                    if total_dur > 0:
                        time_pct = min(99.0, (time_s / total_dur) * 100)
                    # 方法2: 基于输出文件大小的估算进度（对 NVENC 更准确）
                    size_pct = 0.0
                    if src_size > 0 and os.path.exists(encoded_path):
                        size_pct = min(99.0, (os.path.getsize(encoded_path) / src_size) * 100)
                    # 取两者较大值，确保进度不会因单一指标滞后而偏低
                    pct = max(time_pct, size_pct)
                    # 每 2% 更新一次（从 5% 降低，提高刷新频率）
                    if pct - last_pct >= 2 or pct >= 99:
                        last_pct = pct
                        with _transcode_lock:
                            _transcode_progress[video_path] = pct
                except (ValueError, IndexError):
                    pass
        proc.wait(timeout=60)
        with _transcode_lock:
            _transcode_progress[video_path] = 100.0
        if proc.returncode == 0 and os.path.exists(encoded_path):
            size_orig = os.path.getsize(video_path) / (1024*1024)
            size_enc = os.path.getsize(encoded_path) / (1024*1024)
            logger.info(f"[预转码] ✓ 完成: {os.path.basename(video_path)} " + f"({size_orig:.1f}MB → {size_enc:.1f}MB)")
            if _config.get("delete_original_after_transcode", True):
                try:
                    os.remove(video_path)
                    logger.info(f"[预转码] ✓ 已删除原文件: {os.path.basename(video_path)}")
                except Exception as e:
                    logger.info(f"[预转码] ✗ 删除原文件失败: {e}")
            thumb_result = generate_thumbnail(encoded_path, verbose=True)
            if thumb_result == 'created':
                logger.info(f"[预转码] ✓ 已生成缩略图: {os.path.basename(encoded_path)}")
            elif thumb_result == 'cached':
                logger.info(f"[预转码] ○ 缩略图已缓存: {os.path.basename(encoded_path)}")
            elif thumb_result is None:
                logger.info(f"[预转码] ✗ 缩略图生成失败: {os.path.basename(encoded_path)}")
            return True
        else:
            logger.info(f"[预转码] ✗ 失败: {os.path.basename(video_path)} (returncode={proc.returncode})")
            # 转码失败时清理可能已生成的半成品文件
            if os.path.exists(encoded_path):
                try:
                    os.remove(encoded_path)
                    logger.info(f"[预转码] ✗ 已清理半成品文件: {os.path.basename(encoded_path)}")
                except OSError as e:
                    logger.info(f"[预转码] ✗ 清理半成品失败: {e}")
    except subprocess.TimeoutExpired:
        logger.info(f"[预转码] ✗ 超时: {os.path.basename(video_path)}")
        # 超时时也要清理半成品和终止进程
        if os.path.exists(encoded_path):
            try:
                os.remove(encoded_path)
            except OSError:
                pass
    except Exception as e:
        logger.info(f"[预转码] ✗ 异常: {os.path.basename(video_path)} - {e}")
        if os.path.exists(encoded_path):
            try:
                os.remove(encoded_path)
            except OSError:
                pass
    return False

def _transcode_worker():
    """后台转码工作线程：从队列获取任务并执行"""
    first_run = True
    while True:
        time.sleep(10 if first_run and is_auto_scan_enabled() else 30)
        first_run = False
        try:
            # 阶段1：队列空时主动扫描（仅在自动转码开启时）
            with _transcode_lock:
                if not _transcode_queue and is_auto_scan_enabled() and check_ffmpeg() and _config.get("auto_transcode", True):
                    all_videos = _get_all_video_paths()
                    pending = [v for v in all_videos if not os.path.exists(_get_encoded_path(v))]
                    if pending:
                        logger.info(f"[预转码] 队列为空，扫描到 {len(pending)} 个待转码视频")
                    for video_path in pending:
                        if video_path not in _transcode_queue and video_path not in _transcode_in_progress:
                            _transcode_queue.append(video_path)

            # 阶段2：取出一个任务执行
            with _transcode_lock:
                if not _transcode_queue:
                    continue
                video_path = _transcode_queue.pop(0)
                _transcode_in_progress[video_path] = True

            try:
                success = _transcode_video_file(video_path)
                if success:
                    logger.info(f"[预转码] ✓ 已转码: {os.path.basename(video_path)}")
                    _remove_pending_transcode(video_path)
                    mp4_path = os.path.splitext(video_path)[0] + '.mp4'
                    # 用转码后的 .mp4 路径更新缓存（原文件可能已被删除）
                    cache_update_path = mp4_path if os.path.exists(mp4_path) else video_path
                    if os.path.exists(mp4_path):
                        generate_thumbnail(mp4_path)
                    # 定向更新转码文件所在目录的缓存（内部处理锁 + 持久化）
                    try:
                        _update_series_cache_for_video(cache_update_path)
                        logger.info(f"[预转码] ✓ 已更新缓存: {os.path.basename(cache_update_path)}")
                    except Exception as scan_err:
                        logger.info(f"[预转码] ✗ 更新缓存失败: {scan_err}")
            except Exception as e:
                logger.info(f"[预转码] ✗ 转码异常: {os.path.basename(video_path)} - {e}")
            finally:
                with _transcode_lock:
                    _transcode_in_progress.pop(video_path, None)
                    _transcode_progress.pop(video_path, None)
        except Exception as e:
            logger.info(f"[预转码] 后台任务异常: {e}")
            time.sleep(10)


from contextlib import asynccontextmanager

@asynccontextmanager
async def _lifespan(app):  # app 保留：asynccontextmanager 签名要求
    del app
    logger.info("[_lifespan] 入口")
    global _server_start_time
    _server_start_time = time.time()
    threading.Thread(target=_background_init, daemon=True).start()
    global _transcode_worker_started
    with _transcode_lock:
        if not _transcode_worker_started:
            _transcode_worker_started = True
            threading.Thread(target=_transcode_worker, daemon=True, name="TranscodeWorker").start()
    yield


app.router.lifespan_context = _lifespan


if __name__ == '__main__':
    import uvicorn

    # 文件日志处理器（持久化记录）
    _file_handler = logging.FileHandler('mycinema.log', encoding='utf-8')
    _file_handler.setLevel(logging.INFO)
    _file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(_file_handler)
    
    logger.info("[系统] 正在初始化...")
    load_thumbnail_cache()
    load_series_cache()
    check_ffmpeg()

    # 尝试解析 server_host，失败时降级为 127.0.0.1
    bind_host = _server_host
    try:
        import socket
        socket.getaddrinfo(bind_host, _server_port)
    except Exception:
        logger.warning(f"[系统] 无法绑定地址 {bind_host}:{_server_port}，降级为 127.0.0.1")
        bind_host = "127.0.0.1"

    logger.info(f"[系统] 服务已就绪，访问 http://127.0.0.1:{_server_port}")
    uvicorn.run(
        app,
        host=bind_host,
        port=_server_port,
        reload=False,
        log_level="warning",
        access_log=False,
    )
