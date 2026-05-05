import logging
_pb_logger = logging.getLogger("mycinema.pb")
"""Protobuf 序列化/反序列化工具模块

提供 dict <-> Protobuf message 之间的转换，以及原子读写 .pb 文件的功能。
内存中的数据结构保持 dict 不变，仅在文件 I/O 边界进行转换。
"""

import os
import json
import threading

import config_pb2
import categories_pb2
import hidden_series_pb2
import thumbnail_cache_pb2
import video_meta_cache_pb2
import series_cache_pb2
import export_info_pb2

# ====================== 通用 Protobuf 文件 I/O ======================

_pb_write_lock = threading.Lock()


def _atomic_write_pb(filepath, message):
    """原子写入 Protobuf 二进制文件（先写临时文件，再重命名）"""
    tmp_path = filepath + '.tmp'
    try:
        with _pb_write_lock:
            data = message.SerializeToString()
            with open(tmp_path, 'wb') as f:
                f.write(data)
            if os.path.exists(filepath):
                os.replace(tmp_path, filepath)
            else:
                os.rename(tmp_path, filepath)
    except Exception as e:
        _pb_logger.info(f"[pb] 原子写入失败 {filepath}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _load_pb(filepath, message_class):
    """从 Protobuf 二进制文件加载，失败返回 None"""
    try:
        if os.path.exists(filepath):
            with open(filepath, 'rb') as f:
                data = f.read()
            if data:
                msg = message_class()
                msg.ParseFromString(data)
                return msg
    except Exception as e:
        _pb_logger.info(f"[pb] 加载失败 {filepath}: {e}")
    return None


# ====================== Config ======================

def dict_to_config(d):
    msg = config_pb2.Config()
    msg.auto_scan_enabled = d.get("auto_scan_enabled", False)
    msg.delete_original_after_transcode = d.get("delete_original_after_transcode", True)
    msg.idle_check_interval = d.get("idle_check_interval", 300)
    msg.watcher_interval = d.get("watcher_interval", 5)
    msg.cache_ttl = d.get("cache_ttl", 300)
    msg.meta_workers = d.get("meta_workers", 4)
    msg.detail_workers = d.get("detail_workers", 8)
    msg.meta_cache_max = d.get("meta_cache_max", 50000)
    msg.gpu_transcode = d.get("gpu_transcode", True)
    msg.server_port = d.get("server_port", 5000)
    msg.server_host = d.get("server_host", "0.0.0.0")
    msg.prefetch_enabled = d.get("prefetch_enabled", True)
    msg.cdn_cache_max_age = d.get("cdn_cache_max_age", 86400)
    msg.l1_cache_max_size_mb = d.get("l1_cache_max_size_mb", 50)
    msg.l1_cache_ttl = d.get("l1_cache_ttl", 600)
    dc = d.get("default_category")
    if dc:
        msg.default_category.CopyFrom(dict_to_category(dc))
    msg.uncategorized_label = d.get("uncategorized_label", "")
    msg.auto_transcode = d.get("auto_transcode", True)
    return msg


def config_to_dict(msg):
    d = {
        "auto_scan_enabled": msg.auto_scan_enabled,
        "delete_original_after_transcode": msg.delete_original_after_transcode,
        "idle_check_interval": msg.idle_check_interval,
        "watcher_interval": msg.watcher_interval,
        "cache_ttl": msg.cache_ttl,
        "meta_workers": msg.meta_workers,
        "detail_workers": msg.detail_workers,
        "meta_cache_max": msg.meta_cache_max,
        "gpu_transcode": msg.gpu_transcode,
        "server_port": msg.server_port,
        "server_host": msg.server_host,
        "prefetch_enabled": msg.prefetch_enabled,
        "cdn_cache_max_age": msg.cdn_cache_max_age,
        "l1_cache_max_size_mb": msg.l1_cache_max_size_mb,
        "l1_cache_ttl": msg.l1_cache_ttl,
    }
    if msg.HasField("default_category"):
        d["default_category"] = category_to_dict(msg.default_category)
    if msg.uncategorized_label:
        d["uncategorized_label"] = msg.uncategorized_label
    d["auto_transcode"] = msg.auto_transcode
    return d


def read_config(filepath):
    """从 .pb 文件读取配置，返回 dict 或 None"""
    msg = _load_pb(filepath, config_pb2.Config)
    return config_to_dict(msg) if msg else None


def write_config(filepath, config_dict):
    """将配置 dict 写入 .pb 文件"""
    _atomic_write_pb(filepath, dict_to_config(config_dict))


# ====================== Categories ======================

def dict_to_category(d):
    msg = categories_pb2.Category()
    msg.id = d.get("id", "")
    msg.name = d.get("name", "")
    msg.icon = d.get("icon", "")
    msg.color = d.get("color", "")
    msg.dirs.extend(d.get("dirs", []))
    return msg


def category_to_dict(msg):
    return {
        "id": msg.id,
        "name": msg.name,
        "icon": msg.icon,
        "color": msg.color,
        "dirs": list(msg.dirs),
    }


def dict_to_categories_file(cats):
    msg = categories_pb2.CategoriesFile()
    for cat in cats:
        msg.categories.append(dict_to_category(cat))
    return msg


def categories_file_to_list(msg):
    return [category_to_dict(c) for c in msg.categories]


def read_categories(filepath):
    """从 .pb 文件读取分类列表，返回 list[dict] 或 None"""
    msg = _load_pb(filepath, categories_pb2.CategoriesFile)
    return categories_file_to_list(msg) if msg else None


def write_categories(filepath, cats_list):
    """将分类列表写入 .pb 文件"""
    _atomic_write_pb(filepath, dict_to_categories_file(cats_list))


# ====================== HiddenSeries ======================

def paths_to_hidden_series(paths):
    msg = hidden_series_pb2.HiddenSeriesFile()
    msg.paths.extend(paths)
    return msg


def hidden_series_to_paths(msg):
    return list(msg.paths)


def read_hidden_series(filepath):
    """从 .pb 文件读取隐藏列表，返回 list[str]"""
    msg = _load_pb(filepath, hidden_series_pb2.HiddenSeriesFile)
    return hidden_series_to_paths(msg) if msg else []


def write_hidden_series(filepath, paths_list):
    """将隐藏列表写入 .pb 文件"""
    _atomic_write_pb(filepath, paths_to_hidden_series(paths_list))


# ====================== ThumbnailCache ======================

def dict_to_thumbnail_cache(d):
    msg = thumbnail_cache_pb2.ThumbnailCacheFile()
    for key, val in d.items():
        entry = msg.entries[key]
        entry.thumb_path = val.get("thumb_path", "")
        entry.mtime = val.get("mtime", 0.0)
    return msg


def thumbnail_cache_to_dict(msg):
    return {k: {"thumb_path": v.thumb_path, "mtime": v.mtime}
            for k, v in msg.entries.items()}


def read_thumbnail_cache(filepath):
    """从 .pb 文件读取缩略图缓存，返回 dict"""
    msg = _load_pb(filepath, thumbnail_cache_pb2.ThumbnailCacheFile)
    return thumbnail_cache_to_dict(msg) if msg else {}


def write_thumbnail_cache(filepath, cache_dict):
    """将缩略图缓存写入 .pb 文件"""
    _atomic_write_pb(filepath, dict_to_thumbnail_cache(cache_dict))


# ====================== VideoMetaCache ======================

def dict_to_video_meta_cache(d):
    msg = video_meta_cache_pb2.VideoMetaCacheFile()
    for key, val in d.items():
        entry = msg.entries[key]
        entry.mtime = val.get("_mtime", 0)
        entry.orientation = val.get("orientation", "")
        entry.width = val.get("width", 0)
        entry.height = val.get("height", 0)
        entry.resolution = val.get("resolution", "")
        entry.file_size = val.get("file_size", 0)
        entry.duration = val.get("duration", 0.0)
    return msg


def video_meta_cache_to_dict(msg):
    result = {}
    for k, v in msg.entries.items():
        result[k] = {
            "_mtime": v.mtime,
            "orientation": v.orientation,
            "width": v.width,
            "height": v.height,
            "resolution": v.resolution,
            "file_size": v.file_size,
            "duration": v.duration,
        }
    return result


def read_video_meta_cache(filepath):
    """从 .pb 文件读取视频元数据缓存，返回 dict"""
    msg = _load_pb(filepath, video_meta_cache_pb2.VideoMetaCacheFile)
    return video_meta_cache_to_dict(msg) if msg else {}


def write_video_meta_cache(filepath, cache_dict):
    """将视频元数据缓存写入 .pb 文件"""
    _atomic_write_pb(filepath, dict_to_video_meta_cache(cache_dict))


# ====================== SeriesCache ======================

def dict_to_video_entry(d):
    msg = series_cache_pb2.VideoEntry()
    msg.filename = d.get("filename", "")
    msg.filepath = d.get("filepath", "")
    msg.thumbnail = d.get("thumbnail", "")
    msg.resolution = d.get("resolution", "")
    msg.needs_transcode = d.get("needs_transcode", False)
    return msg


def video_entry_to_dict(msg):
    d = {"filename": msg.filename, "filepath": msg.filepath}
    if msg.thumbnail:
        d["thumbnail"] = msg.thumbnail
    if msg.resolution:
        d["resolution"] = msg.resolution
    if msg.needs_transcode:
        d["needs_transcode"] = True
    return d


def dict_to_series_entry(d):
    msg = series_cache_pb2.SeriesEntry()
    msg.name = d.get("name", "")
    msg.path = d.get("path", "")
    msg.total_episodes = d.get("total_episodes", 0)
    msg.episode_count = d.get("episode_count", 0)
    for v in d.get("videos", []):
        msg.videos.append(dict_to_video_entry(v))
    msg.cover = d.get("cover", "")
    msg.orientation = d.get("orientation", "")
    if d.get("category"):
        msg.category.CopyFrom(dict_to_category(d["category"]))
    msg.is_abs = d.get("_is_abs", False)
    msg.abs_path = d.get("_abs_path", "")
    msg.flat_base_path = d.get("_flat_base_path", "")
    msg.category_merged = d.get("_category_merged", False)
    return msg


def series_entry_to_dict(msg):
    d = {
        "name": msg.name,
        "path": msg.path,
        "total_episodes": msg.total_episodes,
        "episode_count": msg.episode_count,
        "videos": [video_entry_to_dict(v) for v in msg.videos],
        "orientation": msg.orientation,
    }
    if msg.cover:
        d["cover"] = msg.cover
    if msg.HasField("category"):
        d["category"] = category_to_dict(msg.category)
    if msg.is_abs:
        d["_is_abs"] = True
    if msg.abs_path:
        d["_abs_path"] = msg.abs_path
    if msg.flat_base_path:
        d["_flat_base_path"] = msg.flat_base_path
    if msg.category_merged:
        d["_category_merged"] = True
    return d


def dict_to_series_cache(d):
    msg = series_cache_pb2.SeriesCacheFile()
    for dir_path, val in d.items():
        entry = msg.dirs[dir_path]
        entry.mtime = val.get("mtime", 0.0)
        data = val.get("data")
        if isinstance(data, list):
            for item in data:
                entry.multiple.items.append(dict_to_series_entry(item))
        elif isinstance(data, dict):
            entry.single.CopyFrom(dict_to_series_entry(data))
    return msg


def series_cache_to_dict(msg):
    result = {}
    for dir_path, val in msg.dirs.items():
        entry = {"mtime": val.mtime}
        which = val.WhichOneof("series_data")
        if which == "single":
            entry["data"] = series_entry_to_dict(val.single)
        elif which == "multiple":
            entry["data"] = [series_entry_to_dict(item) for item in val.multiple.items]
        result[dir_path] = entry
    return result


def read_series_cache(filepath):
    """从 .pb 文件读取系列缓存，返回 dict"""
    msg = _load_pb(filepath, series_cache_pb2.SeriesCacheFile)
    return series_cache_to_dict(msg) if msg else {}


def write_series_cache(filepath, cache_dict):
    """将系列缓存写入 .pb 文件"""
    _atomic_write_pb(filepath, dict_to_series_cache(cache_dict))


# ====================== ExportInfo ======================

def dict_to_export_info(d):
    msg = export_info_pb2.ExportInfo()
    for cat in d.get("categories", []):
        msg.categories.append(dict_to_category(cat))
    msg.auto_scan_enabled = d.get("auto_scan_enabled", False)
    msg.export_time = d.get("export_time", "")
    return msg


def export_info_to_dict(msg):
    return {
        "categories": [category_to_dict(c) for c in msg.categories],
        "auto_scan_enabled": msg.auto_scan_enabled,
        "export_time": msg.export_time,
    }


def read_export_info(filepath):
    """从 .pb 文件读取导出信息，返回 dict 或 None"""
    msg = _load_pb(filepath, export_info_pb2.ExportInfo)
    return export_info_to_dict(msg) if msg else None


def write_export_info(filepath, info_dict):
    """将导出信息写入 .pb 文件"""
    _atomic_write_pb(filepath, dict_to_export_info(info_dict))


# ====================== JSON → Protobuf 迁移 ======================

def migrate_json_to_pb(json_path, pb_path, convert_fn):
    """将 JSON 文件迁移为 Protobuf 二进制格式（仅在 .json 存在且 .pb 不存在时执行）"""
    if not os.path.exists(json_path) or os.path.exists(pb_path):
        return False
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        msg = convert_fn(data)
        _atomic_write_pb(pb_path, msg)
        _pb_logger.info(f"[迁移] {os.path.basename(json_path)} -> {os.path.basename(pb_path)}")
        return True
    except Exception as e:
        _pb_logger.info(f"[迁移] 失败 {json_path}: {e}")
        return False


def run_migration(base_dir):
    """运行 JSON -> Protobuf 迁移（首次启动时自动执行）"""
    # config.json -> config.pb
    migrate_json_to_pb(
        os.path.join(base_dir, "config.json"),
        os.path.join(base_dir, "config.pb"),
        dict_to_config,
    )
    # categories.json -> categories.pb
    def _cats_conv(d):
        return dict_to_categories_file(d.get("categories", []))
    migrate_json_to_pb(
        os.path.join(base_dir, "categories.json"),
        os.path.join(base_dir, "categories.pb"),
        _cats_conv,
    )
    # hidden_series.json -> hidden_series.pb
    migrate_json_to_pb(
        os.path.join(base_dir, "hidden_series.json"),
        os.path.join(base_dir, "hidden_series.pb"),
        lambda d: paths_to_hidden_series(d.get("paths", [])),
    )
    # thumbnail_cache.json -> thumbnail_cache.pb
    migrate_json_to_pb(
        os.path.join(base_dir, "thumbnail_cache.json"),
        os.path.join(base_dir, "thumbnail_cache.pb"),
        dict_to_thumbnail_cache,
    )
    # video_meta_cache.json -> video_meta_cache.pb
    migrate_json_to_pb(
        os.path.join(base_dir, "video_meta_cache.json"),
        os.path.join(base_dir, "video_meta_cache.pb"),
        dict_to_video_meta_cache,
    )
    # series_cache.json -> series_cache.pb
    migrate_json_to_pb(
        os.path.join(base_dir, "series_cache.json"),
        os.path.join(base_dir, "series_cache.pb"),
        dict_to_series_cache,
    )
    # export_info.json -> export_info.pb (在 backup_latest 中)
    backup_dir = os.path.join(base_dir, "backup_latest")
    migrate_json_to_pb(
        os.path.join(backup_dir, "export_info.json"),
        os.path.join(backup_dir, "export_info.pb"),
        dict_to_export_info,
    )
