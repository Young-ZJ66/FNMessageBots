"""硬盘采集：lsblk/findmnt/smartctl/sysfs 多源解析、容量与健康温度。"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import stat as stat_mod
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore

from ._consts import (
    _PATROL_CONTAINER_APP_MOUNT_PREFIXES,
    _PATROL_CONTAINER_FILE_BIND_MOUNTS,
    _PATROL_HOST_STORAGE_PATH_CANDIDATES,
    _PATROL_PSUTIL_NOISE_FSTYPES,
)
from .probes import (
    _resolve_cmd,
    _run_cmd,
    _safe_glob,
    _safe_path_exists,
    _valid_sysfs_block_token,
)



# smartctl -n standby：盘处于低功耗态时的提示（不唤醒盘）。
# 文本形如 "Device is in STANDBY mode, exit(2)" / "Device is in a low-power mode (STANDBY)"
_SMART_STANDBY_RE = re.compile(
    r"device is in (a )?(standby|sleep|zzz|low-power)|standby mode, exit", re.IGNORECASE
)


def _normalize_block_name(name: str) -> str:
    n = str(name or "").strip()
    if not n or any(ch in n for ch in ":/\\"):
        return ""
    # trim_/luks- 逻辑名不做末尾去数字，避免把 UUID 段误削坏；解析应走 mapper/realpath
    if n.startswith(("trim_", "luks-")):
        return n
    # device mapper: dm-2 不能去掉末尾数字
    if re.fullmatch(r"dm-\d+", n):
        return n
    # nvme0n1p2 -> nvme0n1；nvme0n1 必须保持（禁止误削成 nvme0n）
    m_nv = re.fullmatch(r"(nvme\d+n\d+)(p\d+)?", n, re.I)
    if m_nv:
        return m_nv.group(1)
    # mmcblk0p1 -> mmcblk0
    if n.startswith("mmcblk") and "p" in n:
        return n.split("p")[0]
    # md RAID 设备保留编号（例如 md0）
    if re.fullmatch(r"md\d+", n):
        return n
    # sda1 -> sda
    return re.sub(r"\d+$", "", n)


def _patrol_block_dev_for_inspection(dev_path: str) -> str:
    """把 ``/dev/trim_*`` 等规范成 ``/dev/mapper/…`` 或 ``dm-*`` 节点，便于 lsblk/sysfs 解析到 sda/nvme。"""
    dp = str(dev_path or "").strip()
    if not dp.startswith("/dev/"):
        return dp
    candidates: List[str] = []
    seen: Set[str] = set()

    def _add(p: str) -> None:
        p = str(p).strip()
        if p.startswith("/dev/") and p not in seen:
            seen.add(p)
            candidates.append(p)

    _add(dp)
    try:
        _add(os.path.realpath(dp))
    except OSError:
        pass
    base = os.path.basename(dp)
    if base.startswith(("trim_", "luks-")):
        mp = f"/dev/mapper/{base}"
        if mp != dp:
            _add(mp)
            try:
                _add(os.path.realpath(mp))
            except OSError:
                pass
    for c in candidates:
        try:
            bn = os.path.basename(os.path.realpath(c))
        except OSError:
            bn = os.path.basename(c)
        if re.fullmatch(r"dm-\d+", bn):
            try:
                return os.path.realpath(c)
            except OSError:
                return c
    for c in candidates:
        if c.startswith("/dev/"):
            try:
                return os.path.realpath(c)
            except OSError:
                return c
    return dp


def _lsblk_pkname_devpath(dev_path: str) -> str:
    """对真实块设备路径查询 lsblk PKNAME（必须用 /dev/mapper/trim_… 等完整路径，不能拼成 /dev/trim_…）。"""
    dp = str(dev_path or "").strip()
    if not dp.startswith("/dev/"):
        return ""
    lsblk = _resolve_cmd("lsblk")
    try:
        proc = subprocess.run(
            [lsblk, "-no", "PKNAME", dp],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return ""
    tok = line[0].strip()
    if tok and tok not in ("-", "--") and _valid_sysfs_block_token(tok):
        return tok
    base = os.path.basename(dp)
    if base.startswith(("trim_", "luks-")):
        mp = f"/dev/mapper/{base}"
        if mp != dp and mp.startswith("/dev/"):
            return _lsblk_pkname_devpath(mp)
    return ""


def _patrol_is_whole_disk_name(nn: str) -> bool:
    """已规范化到整盘名（非 mapper/trim 逻辑名）。"""
    if not nn or not _valid_sysfs_block_token(nn):
        return False
    if re.fullmatch(r"nvme\d+n\d+", nn, re.I):
        return True
    if re.fullmatch(r"sd[a-z]+", nn, re.I):
        return True
    if re.fullmatch(r"vd[a-z]+", nn, re.I):
        return True
    if re.fullmatch(r"xvd[a-z]+", nn, re.I):
        return True
    if re.fullmatch(r"hd[a-z]+", nn, re.I):
        return True
    if re.fullmatch(r"mmcblk\d+", nn, re.I):
        return True
    return False


def _walk_lsblk_to_physical_base(dev_path: str) -> str:
    """沿 PKNAME 从挂载点对应设备解析到 nvme*/sd* 等整盘名（解决 trim_* 显示名问题）。"""
    try:
        cur = _patrol_block_dev_for_inspection(dev_path)
    except Exception:
        return ""
    if not str(cur).startswith("/dev/"):
        return ""
    seen: Set[str] = set()
    for _ in range(40):
        if cur in seen:
            break
        seen.add(cur)
        nm = os.path.basename(cur)
        nn = _normalize_block_name(nm) or nm
        if not nn:
            break
        if not _valid_sysfs_block_token(nn):
            break
        if _patrol_is_whole_disk_name(nn):
            return nn
        pk = _lsblk_pkname_devpath(cur)
        if not pk:
            break
        cur = pk if pk.startswith("/dev/") else f"/dev/{pk}"
    return ""


def _lsblk_parent(name_or_path: str) -> str:
    if not name_or_path:
        return ""
    if str(name_or_path).startswith("/dev/"):
        return _lsblk_pkname_devpath(name_or_path)
    token = name_or_path.removeprefix("/dev/").strip()
    if not _valid_sysfs_block_token(token):
        return ""
    for dev in (f"/dev/{token}", f"/dev/mapper/{token}"):
        pk = _lsblk_pkname_devpath(dev)
        if pk:
            return pk
    return ""


def _list_sysfs_slaves(block_name: str) -> List[str]:
    if not block_name:
        return []
    slaves_dir = Path(f"/sys/block/{block_name}/slaves")
    try:
        names = sorted([p.name for p in slaves_dir.iterdir()])
    except Exception:
        return []
    return [n for n in names if n]


def _dedupe_block_names(names: List[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for raw in names:
        name = _normalize_block_name(raw)
        if not name or not _valid_sysfs_block_token(name):
            continue
        if not _patrol_is_whole_disk_name(name):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _resolve_block_to_physical_names(block_name: str, visited: Optional[set] = None) -> List[str]:
    """递归将 dm/md/分区设备解析为所有底层物理盘，避免多盘存储池只展示第一块。"""
    name = _normalize_block_name(block_name)
    if not name or not _valid_sysfs_block_token(name):
        return []
    if _patrol_is_whole_disk_name(name):
        return [name]

    if visited is None:
        visited = set()
    if name in visited:
        return []
    visited.add(name)

    names: List[str] = []
    for slave in _list_sysfs_slaves(name):
        names.extend(_resolve_block_to_physical_names(slave, visited))
    if names:
        return _dedupe_block_names(names)

    parent = _lsblk_parent(name)
    if parent:
        names.extend(_resolve_block_to_physical_names(parent, visited))
    return _dedupe_block_names(names)


def _resolve_block_to_physical(block_name: str, visited: Optional[set] = None) -> str:
    """递归将 dm/md/分区设备解析到底层物理盘（sd/nvme/mmcblk）。"""
    names = _resolve_block_to_physical_names(block_name, visited)
    if names:
        return names[0]
    name = _normalize_block_name(block_name)
    if not name:
        return ""

    if visited is None:
        visited = set()
    if name in visited:
        return name
    visited.add(name)

    # 先走 sysfs slaves（适合 dm/md）
    slaves = _list_sysfs_slaves(name)
    for s in slaves:
        resolved = _resolve_block_to_physical(s, visited)
        if resolved.startswith(("sd", "hd", "vd", "xvd", "nvme", "mmcblk")):
            return resolved

    # 再走 lsblk 父链
    parent = _lsblk_parent(name)
    if parent:
        return _resolve_block_to_physical(parent, visited)
    return name


def _sysfs_block_basename_from_devpath(dev_path: str) -> str:
    """通过 /sys/dev/block/M:m 解析块设备在 sysfs 下的名字（如 dm-2、sda1），不依赖路径是否带 mapper/trim 前缀。"""
    dp0 = str(dev_path or "").strip()
    if not dp0.startswith("/dev/"):
        return ""
    cand_list: List[str] = []
    seen_c: Set[str] = set()
    for c in (
        _patrol_block_dev_for_inspection(dp0),
        dp0,
    ):
        if not c.startswith("/dev/") or c in seen_c:
            continue
        seen_c.add(c)
        cand_list.append(c)
        try:
            rp = os.path.realpath(c)
            if rp.startswith("/dev/") and rp not in seen_c:
                seen_c.add(rp)
                cand_list.append(rp)
        except OSError:
            pass
    for cand in cand_list:
        if not str(cand).startswith("/dev/"):
            continue
        try:
            st = os.stat(cand)
        except OSError:
            continue
        if not stat_mod.S_ISBLK(st.st_mode):
            continue
        maj = os.major(st.st_rdev)
        mi = os.minor(st.st_rdev)
        link = Path(f"/sys/dev/block/{maj}:{mi}")
        try:
            if not link.exists():
                continue
            name = link.resolve().name
            if _valid_sysfs_block_token(name):
                return name
        except OSError:
            continue
    return ""


def _resolve_physical_disk_name(dev_path: str) -> str:
    names = _resolve_physical_disk_names(dev_path)
    if names:
        return names[0]
    return ""


def _resolve_physical_disk_names(dev_path: str) -> List[str]:
    if not dev_path or not str(dev_path).startswith("/dev/"):
        return []
    found: List[str] = []
    walked = _walk_lsblk_to_physical_base(dev_path)
    if walked:
        found.append(walked)
    sb = _sysfs_block_basename_from_devpath(dev_path)
    if sb:
        found.extend(_resolve_block_to_physical_names(sb))
    insp = _patrol_block_dev_for_inspection(dev_path)
    try:
        base = os.path.basename(os.path.realpath(insp))
    except OSError:
        base = os.path.basename(insp)
    base = _normalize_block_name(base) or base
    found.extend(_resolve_block_to_physical_names(base))
    return _dedupe_block_names(found)


def _patrol_mount_is_file_bind_noise(mount: str) -> bool:
    """容器内常见文件级 bind mount 不是数据卷，不能作为硬盘行展示。"""
    m = str(mount or "").strip()
    if not m:
        return False
    if m != "/":
        m = m.rstrip("/")
    if m in _PATROL_CONTAINER_FILE_BIND_MOUNTS:
        return True
    if any(m == p or m.startswith(f"{p}/") for p in _PATROL_CONTAINER_APP_MOUNT_PREFIXES):
        return True
    if m.startswith(("/run/secrets/", "/var/run/secrets/")):
        return True
    try:
        st = os.stat(m)
    except OSError:
        return False
    return stat_mod.S_ISREG(st.st_mode)


def _patrol_readable_unresolved_device_label(dev_path: str, mount: str) -> str:
    """物理盘解析失败时仍避免展示 trim_/luks- 这类内部映射 ID。"""
    m = str(mount or "").strip().rstrip("/")
    if _patrol_mount_is_file_bind_noise(m):
        return "存储空间"
    if m:
        tail = os.path.basename(m) or m.strip("/")
        if tail:
            return f"存储空间 {tail}"
    raw = os.path.basename(str(dev_path or "").strip())
    if raw.startswith(("trim_", "luks-")):
        return "存储空间"
    return raw or "unknown"


def _smart_health_for_block_path(block_path: str) -> str:
    """对 ``/dev/...`` 绝对路径做 SMART 健康判断（含 mapper、分区）。"""
    bp = str(block_path or "").strip()
    if not bp.startswith("/dev/"):
        return "--"
    base = os.path.basename(bp)
    nb = _normalize_block_name(base) or base
    if not _valid_sysfs_block_token(nb) and not bp.startswith("/dev/mapper/"):
        return "--"
    # -n standby：休眠盘直接返回低功耗提示而不唤醒（温度/健康在休眠态本就无意义）
    out = _run_cmd(["smartctl", "-n", "standby", "-H", bp], timeout=2.5)
    if out:
        low = out.lower()
        if _SMART_STANDBY_RE.search(low):
            return "休眠"
        if "test result: passed" in low or "smart overall-health self-assessment test result: passed" in low:
            return "健康"
        fail_markers = (
            "test result: failed",
            "overall-health self-assessment test result: failed",
            "smart overall-health self-assessment test result: failed",
            "prefail",
        )
        if any(k in low for k in fail_markers):
            return "异常"
        m = re.search(r"critical_warning\s*[:=]\s*(0x[0-9a-fA-F]+|\d+)", out)
        if m:
            raw = m.group(1).strip().lower()
            try:
                val = int(raw, 16) if raw.startswith("0x") else int(raw)
                return "健康" if val == 0 else "异常"
            except Exception:
                pass

    if "nvme" in nb.lower():
        out_nvme_json = _run_cmd(["nvme", "smart-log", "-o", "json", bp], timeout=2.5)
        if out_nvme_json:
            try:
                obj = json.loads(out_nvme_json)
                if isinstance(obj, dict):
                    cw = obj.get("critical_warning")
                    if cw is not None:
                        v = int(cw, 16) if isinstance(cw, str) and cw.lower().startswith("0x") else int(cw)
                        return "健康" if v == 0 else "异常"
            except Exception:
                pass
        out_nvme = _run_cmd(["nvme", "smart-log", bp], timeout=2.5)
        if out_nvme:
            m = re.search(r"critical_warning\s*[:=]\s*(0x[0-9a-fA-F]+|\d+)", out_nvme)
            if m:
                raw = m.group(1).strip().lower()
                try:
                    val = int(raw, 16) if raw.startswith("0x") else int(raw)
                    return "健康" if val == 0 else "异常"
                except Exception:
                    pass
        ctrl = nb.split("n")[0] if "n" in nb else nb
        smart_candidates = [
            Path(f"/sys/class/nvme/{ctrl}/smart_log/critical_warning"),
            Path(f"/sys/class/nvme/{ctrl}/device/critical_warning"),
        ]
        for p in smart_candidates:
            try:
                raw = p.read_text(encoding="utf-8", errors="ignore").strip().lower()
                if not raw:
                    continue
                val = int(raw, 16) if raw.startswith("0x") else int(raw)
                return "健康" if val == 0 else "异常"
            except Exception:
                continue

    sys_key = nb if _valid_sysfs_block_token(nb) else ""
    if sys_key:
        sys_state = Path(f"/sys/block/{sys_key}/device/state")
        try:
            s = sys_state.read_text(encoding="utf-8", errors="ignore").strip().lower()
            if s in {"running", "active", "live"}:
                return "健康"
        except Exception:
            pass
    return "--"


def _smart_health_for_disk(dev_base: str) -> str:
    if not dev_base:
        return "--"
    if str(dev_base).startswith("/dev/"):
        return _smart_health_for_block_path(dev_base)
    if not _valid_sysfs_block_token(dev_base):
        return "--"
    return _smart_health_for_block_path(f"/dev/{dev_base}")


def _patrol_sysfs_disk_running(dev_base: str) -> bool:
    """内核块设备 ``running/active/live`` 视为在线可用（无 smartctl 时的弱信号）。"""
    db = str(dev_base or "").strip()
    if not db or not _valid_sysfs_block_token(db):
        return False
    try:
        s = Path(f"/sys/block/{db}/device/state").read_text(encoding="utf-8", errors="ignore").strip().lower()
        return s in {"running", "active", "live"}
    except OSError:
        return False


def _smart_health_for_patrol_partition(dev_path: str, physical: str) -> str:
    """先对挂载点对应块设备路径探测，再对解析出的整盘名（sda/nvme0n1）探测。"""
    try:
        real = os.path.realpath(dev_path)
    except Exception:
        real = dev_path
    if str(real).startswith("/dev/"):
        h = _smart_health_for_block_path(real)
        if h not in {"--", "—"}:
            return h
    if physical and _patrol_is_whole_disk_name(physical):
        h2 = _smart_health_for_block_path(f"/dev/{physical}")
        if h2 not in {"--", "—"}:
            return h2
    if physical and _valid_sysfs_block_token(physical):
        return _smart_health_for_disk(physical)
    return "--"


def _celsius_ok(v: float) -> bool:
    return -40.0 <= float(v) <= 120.0


def _fmt_celsius(v: float) -> str:
    return f"{float(v):.1f}"


def _nvme_kelvin_or_celsius(v: int) -> Optional[float]:
    """NVMe 日志温度可能是摄氏或开尔文。"""
    try:
        x = int(v)
    except (TypeError, ValueError):
        return None
    if x > 200:
        x = x - 273
    if _celsius_ok(x):
        return float(x)
    return None


def _parse_celsius_from_smartctl_json(obj: Any) -> str:
    """与飞牛界面一致：优先复合温度 / Sensor1 / ATA-194 RAW，不用 Sensor2。"""
    if not isinstance(obj, dict):
        return "--"

    # 1) smartctl 顶层 temperature.current（通常等于飞牛主温度）
    temp_obj = obj.get("temperature")
    if isinstance(temp_obj, dict):
        if temp_obj.get("current") is not None:
            try:
                c = float(temp_obj.get("current"))
                if _celsius_ok(c):
                    return _fmt_celsius(c)
            except (TypeError, ValueError):
                pass
        # 若有 sensors 列表，只用第一个（Sensor 1）
        sensors = temp_obj.get("sensors") or temp_obj.get("temperature_sensors")
        if isinstance(sensors, list) and sensors:
            try:
                c = float(sensors[0])
                if c > 200:
                    c = c - 273.0
                if _celsius_ok(c):
                    return _fmt_celsius(c)
            except (TypeError, ValueError):
                pass
    elif temp_obj is not None:
        try:
            c = float(temp_obj)
            if _celsius_ok(c):
                return _fmt_celsius(c)
        except (TypeError, ValueError):
            pass

    # 2) NVMe SMART 健康日志：composite / temperature，再 sensor 1；明确跳过 sensor 2+
    nvme = obj.get("nvme_smart_health_information_log")
    if isinstance(nvme, dict):
        for k in (
            "temperature",
            "composite_temperature",
            "temperature_sensor_1",
            "Temperature Sensor 1",
        ):
            if nvme.get(k) is None:
                continue
            c = _nvme_kelvin_or_celsius(nvme.get(k))
            if c is not None:
                return _fmt_celsius(c)
        # 数组形式：仅取下标 0
        for k in ("temperature_sensors", "sensors"):
            arr = nvme.get(k)
            if isinstance(arr, list) and arr:
                c = _nvme_kelvin_or_celsius(arr[0])
                if c is not None:
                    return _fmt_celsius(c)

    # 3) ATA：优先 194 Temperature_Celsius 的 RAW，其次 190；绝不用归一化 value
    table = (obj.get("ata_smart_attributes") or {}).get("table")
    if isinstance(table, list):
        by_id: Dict[int, Any] = {}
        for attr in table:
            if not isinstance(attr, dict):
                continue
            try:
                aid = int(attr.get("id"))
            except (TypeError, ValueError):
                continue
            by_id[aid] = attr
        for aid in (194, 190):
            attr = by_id.get(aid)
            if not attr:
                continue
            raw = attr.get("raw")
            val = raw.get("value") if isinstance(raw, dict) else raw
            if val is None:
                continue
            try:
                v = int(str(val).split()[0])
                if v > 200:
                    v = v & 0xFF
                if _celsius_ok(v):
                    return _fmt_celsius(v)
            except (TypeError, ValueError):
                continue
    return "--"


def _parse_celsius_from_smartctl_text(out: str) -> str:
    """文本解析：先主 Temperature:，再 Sensor 1；忽略 Sensor 2+；ATA 用 RAW。"""
    main_temp: Optional[str] = None
    sensor1: Optional[str] = None

    for line in (out or "").splitlines():
        low = line.lower().strip()
        if not low:
            continue

        # 跳过统计/告警时间行
        if "comp. temperature time" in low or "temperature time:" in low:
            continue
        # 明确忽略 Sensor 2/3/...
        if re.match(r"temperature\s+sensor\s+[2-9]\d*\s*:", low):
            continue

        # 主温度：Temperature: 41 Celsius（不能写成 Temperature Sensor）
        if re.match(r"^temperature\s*:", low) or low.startswith("current drive temperature"):
            m = re.search(
                r"(?:^temperature|current drive temperature)\s*[:=]\s*(-?\d+)",
                low,
                flags=re.IGNORECASE,
            )
            if m:
                try:
                    v = int(m.group(1))
                    if _celsius_ok(v):
                        main_temp = _fmt_celsius(v)
                except (TypeError, ValueError):
                    pass
            continue

        # Sensor 1
        if re.match(r"temperature\s+sensor\s+1\s*:", low):
            m = re.search(r":\s*(-?\d+)", low)
            if m:
                try:
                    v = int(m.group(1))
                    if _celsius_ok(v):
                        sensor1 = _fmt_celsius(v)
                except (TypeError, ValueError):
                    pass
            continue

        # ATA 194 / 190：取「-」后的 RAW，避免把 VALUE/THRESH 当温度
        if "temperature_celsius" in low or re.match(r"^190\s+", low) or re.match(r"^194\s+", low):
            # 优先 194 行；190 仅作后备（下面用 found 顺序控制）
            m = re.search(r"-\s+(\d+)\s*(?:\(|$)", line)
            if m:
                try:
                    v = int(m.group(1))
                    if _celsius_ok(v):
                        if "temperature_celsius" in low or re.match(r"^194\s+", low):
                            if main_temp is None:
                                main_temp = _fmt_celsius(v)
                        elif sensor1 is None:
                            sensor1 = _fmt_celsius(v)
                except (TypeError, ValueError):
                    pass
            continue

    if main_temp:
        return main_temp
    if sensor1:
        return sensor1
    return "--"


def _sysfs_temp_path_sort_key(p: Path) -> Tuple[int, str]:
    """优先 temp1_input（对应 Sensor1/主温），再 temp2+。"""
    name = p.name.lower()
    m = re.match(r"temp(\d+)_input$", name)
    idx = int(m.group(1)) if m else 99
    # smart_log/temperature 视作主温度
    if name == "temperature":
        idx = 0
    return (idx, str(p))


def _hwmon_temp1_for_block(nb: str) -> str:
    if not _valid_sysfs_block_token(nb):
        return ""
    patterns = (
        f"/sys/block/{nb}/../../hwmon/hwmon*/temp1_input",
        f"/sys/block/{nb}/device/hwmon*/temp1_input",
        f"/sys/block/{nb}/device/hwmon/hwmon*/temp1_input",
    )
    for pat in patterns:
        try:
            matches = sorted(glob.glob(pat))
        except Exception:
            continue
        for f in matches:
            try:
                raw = Path(f).read_text(encoding="utf-8", errors="ignore").strip()
                c = int(raw) // 1000
            except (OSError, ValueError):
                continue
            if _celsius_ok(c):
                return str(c)
    return ""


def _smart_temp_for_block_path(block_path: str) -> str:
    bp = str(block_path or "").strip()
    if not bp.startswith("/dev/"):
        return "--"
    base = os.path.basename(bp)
    nb = _normalize_block_name(base) or base

    # 0) 与飞牛系统显示一致：设备所属控制器 hwmon 的 temp1_input（毫摄氏度取整）
    hw = _hwmon_temp1_for_block(nb)
    if hw:
        return hw

    # 1) smartctl JSON（避免文本列对齐误解析）；-n standby 防止唤醒休眠盘
    out_json = _run_cmd(["smartctl", "-n", "standby", "-A", "-j", bp], timeout=3.0)
    if out_json:
        if _SMART_STANDBY_RE.search(out_json.lower()):
            return "休眠"
        try:
            parsed = _parse_celsius_from_smartctl_json(json.loads(out_json))
            if parsed not in {"--", "—"}:
                return parsed
        except Exception:
            pass

    # 2) smartctl 文本
    out = _run_cmd(["smartctl", "-n", "standby", "-A", bp], timeout=2.5)
    if out:
        if _SMART_STANDBY_RE.search(out.lower()):
            return "休眠"
        parsed = _parse_celsius_from_smartctl_text(out)
        if parsed not in {"--", "—"}:
            return parsed

    # 3) nvme-cli：主温度 / sensor1，不用 sensor2
    if "nvme" in nb.lower():
        out_nvme_json = _run_cmd(["nvme", "smart-log", "-o", "json", bp], timeout=2.5)
        if out_nvme_json:
            try:
                obj = json.loads(out_nvme_json)
                if isinstance(obj, dict):
                    for k in (
                        "temperature",
                        "composite_temperature",
                        "temperature_sensor_1",
                        "temp",
                    ):
                        if obj.get(k) is None:
                            continue
                        c = _nvme_kelvin_or_celsius(obj.get(k))
                        if c is not None:
                            return _fmt_celsius(c)
                    for k in ("temperature_sensors", "sensors"):
                        arr = obj.get(k)
                        if isinstance(arr, list) and arr:
                            c = _nvme_kelvin_or_celsius(arr[0])
                            if c is not None:
                                return _fmt_celsius(c)
            except Exception:
                pass
        out_nvme = _run_cmd(["nvme", "smart-log", bp], timeout=2.5)
        if out_nvme:
            # 只要第一行 temperature:，不要 Temperature Sensor 2
            for line in out_nvme.splitlines():
                low = line.lower().strip()
                if re.match(r"temperature\s+sensor\s+[2-9]", low):
                    continue
                if re.match(r"^temperature\s*:", low) or re.match(r"temperature\s+sensor\s+1\s*:", low):
                    m = re.search(r":\s*(-?\d+)", low)
                    if m:
                        c = _nvme_kelvin_or_celsius(int(m.group(1)))
                        if c is not None:
                            return _fmt_celsius(c)

    # 4) sysfs / hwmon：优先 temp1
    sys_paths: List[Path] = []
    if _valid_sysfs_block_token(nb):
        block_hwmon = Path(f"/sys/block/{nb}/device/hwmon")
        if _safe_path_exists(block_hwmon):
            for p in _safe_glob(block_hwmon, "hwmon*/temp*_input"):
                sys_paths.append(p)
        if nb.startswith("nvme"):
            ctrl = nb.split("n")[0] if "n" in nb else nb
            nvme_hwmon = Path(f"/sys/class/nvme/{ctrl}/device/hwmon")
            if _safe_path_exists(nvme_hwmon):
                for p in _safe_glob(nvme_hwmon, "hwmon*/temp*_input"):
                    sys_paths.append(p)
            sys_paths.append(Path(f"/sys/class/nvme/{ctrl}/smart_log/temperature"))
        try:
            for hm in Path("/sys/class/hwmon").iterdir():
                try:
                    name = (hm / "name").read_text(encoding="utf-8", errors="ignore").strip().lower()
                except OSError:
                    name = ""
                if name not in {"drivetemp", "nvme", "sata"} and nb.lower() not in name:
                    try:
                        link = os.path.realpath(str(hm / "device"))
                        if f"/{nb}" not in link and not link.endswith(f"/{nb}"):
                            continue
                    except OSError:
                        if name not in {"drivetemp", "nvme"}:
                            continue
                for p in _safe_glob(hm, "temp*_input"):
                    sys_paths.append(p)
        except OSError:
            pass

    seen_paths: Set[str] = set()
    ordered = sorted(sys_paths, key=_sysfs_temp_path_sort_key)
    for temp_path in ordered:
        sp = str(temp_path)
        if sp in seen_paths:
            continue
        seen_paths.add(sp)
        # 跳过 temp2+（飞牛展示用 Sensor1/temp1）
        m_idx = re.search(r"temp(\d+)_input$", temp_path.name.lower())
        if m_idx and int(m_idx.group(1)) >= 2:
            continue
        try:
            raw = temp_path.read_text(encoding="utf-8", errors="ignore").strip()
            if not raw:
                continue
            v = int(raw)
            if abs(v) > 1000:
                c = v / 1000.0
            else:
                c = float(v)
            if _celsius_ok(c):
                return _fmt_celsius(c)
        except Exception:
            continue
    return "--"


def _smart_temp_for_disk(dev_base: str) -> str:
    if not dev_base:
        return "--"
    if str(dev_base).startswith("/dev/"):
        return _smart_temp_for_block_path(dev_base)
    if not _valid_sysfs_block_token(dev_base):
        return "--"
    return _smart_temp_for_block_path(f"/dev/{dev_base}")


def _smart_temp_for_patrol_partition(dev_path: str, physical: str) -> str:
    try:
        real = os.path.realpath(dev_path)
    except Exception:
        real = dev_path
    if str(real).startswith("/dev/"):
        t = _smart_temp_for_block_path(real)
        if t not in {"--", "—"}:
            return t
    if physical and _patrol_is_whole_disk_name(physical):
        t2 = _smart_temp_for_block_path(f"/dev/{physical}")
        if t2 not in {"--", "—"}:
            return t2
    if physical and _valid_sysfs_block_token(physical):
        return _smart_temp_for_disk(physical)
    return "--"


def _patrol_resolve_psutil_device(raw_dev: str) -> str:
    """把 psutil 的 device 字段解析成真实 ``/dev/...``（支持 UUID=/LABEL=/PARTUUID=）。"""
    d = str(raw_dev or "").strip()
    if not d:
        return ""
    if d.startswith("/dev/"):
        try:
            if os.path.exists(d):
                return os.path.realpath(d)
        except OSError:
            pass
        return d
    key, _, rest = d.partition("=")
    key = key.strip().lower()
    rest = rest.strip()
    if not key or not rest:
        return ""
    try:
        if key == "uuid":
            for u in (rest.lower(), rest):
                p = Path(f"/dev/disk/by-uuid/{u}")
                if p.exists():
                    return os.path.realpath(str(p))
        if key == "partuuid":
            for u in (rest.lower(), rest):
                p = Path(f"/dev/disk/by-partuuid/{u}")
                if p.exists():
                    return os.path.realpath(str(p))
        if key == "partlabel":
            base = Path("/dev/disk/by-partlabel")
            if base.is_dir():
                cand = base / rest
                if cand.exists():
                    return os.path.realpath(str(cand))
                for child in base.iterdir():
                    if child.name == rest:
                        return os.path.realpath(str(child))
        if key == "label":
            base = Path("/dev/disk/by-label")
            if base.is_dir():
                cand = base / rest
                if cand.exists():
                    return os.path.realpath(str(cand))
                for child in base.iterdir():
                    if child.name == rest:
                        return os.path.realpath(str(child))
    except OSError:
        return ""
    return ""


def _patrol_mount_is_data_volish(mnt: str) -> bool:
    """飞牛多块存储空间典型挂载：/vol1、/vol2、/vol3（大小写不敏感）；亦兼容 /volumeN。"""
    m = str(mnt or "").strip()
    ml = m.lower()
    # 必须 /vol 后紧跟数字，避免误把 /volumes、/voluntary 等当成数据卷
    if re.match(r"^/vol\d+", ml):
        return True
    return bool(re.match(r"^/volume\d+(?:/|$)", m, re.I))


def _patrol_is_fn_vol_mount(mnt: str) -> bool:
    """飞牛「存储空间」标准挂载点：仅为 ``/vol`` + 数字（不含子路径）。"""
    return bool(re.fullmatch(r"/vol\d+", str(mnt or "").strip(), re.I))


# compose 预挂不存在的 /volN 时，Docker 会在宿主建空目录再 bind；内容上不像真实存储空间
_FN_VOL_REAL_NAME_MARKERS = frozenset(
    {
        "@appdata",
        "@homes",
        "@home",
        "@share",
        "@shares",
        "@thumbnail",
        "@tmp",
        "@database",
        "homes",
        "Users",
    }
)


def _patrol_fn_vol_looks_like_real_storage(mnt: str) -> bool:
    """真实飞牛存储卷应含 @appdata 等目录；排除 Docker 为缺失 /volN 创建的空/假 bind。"""
    p = Path(str(mnt or "").strip())
    try:
        if not p.is_dir():
            return False
        for entry in p.iterdir():
            name = entry.name
            if name in _FN_VOL_REAL_NAME_MARKERS or name.startswith("@"):
                return True
        return False
    except OSError:
        return False


def _patrol_findmnt_block_mounts() -> List[Tuple[str, str]]:
    """用 findmnt 补全 psutil 未列出的块设备挂载（Linux 飞牛宿主机常见）。"""
    fm = _resolve_cmd("findmnt")
    try:
        proc = subprocess.run(
            [fm, "-rno", "SOURCE,TARGET,FSTYPE"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    rows: List[Tuple[str, str]] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "\t" in line:
            parts = line.split("\t")
        else:
            parts = re.split(r"\s+", line, maxsplit=2)
        if len(parts) < 2:
            continue
        src, tgt = parts[0].strip(), parts[1].strip()
        fst = parts[2].strip().lower() if len(parts) > 2 else ""
        if not tgt or tgt == "/":
            continue
        if fst in _PATROL_PSUTIL_NOISE_FSTYPES:
            continue
        low = tgt.lower()
        if "docker" in low or "containerd" in low or "/kub" in low:
            continue
        if tgt.startswith(("/proc/", "/sys/", "/run/credentials", "/snap/")):
            continue
        if _patrol_mount_is_file_bind_noise(tgt):
            continue
        if src.startswith("["):
            continue
        rdev = _patrol_resolve_psutil_device(src) if not str(src).startswith("/dev/") else str(src).strip()
        if not rdev.startswith("/dev/"):
            continue
        try:
            rdev = os.path.realpath(rdev)
        except OSError:
            pass
        if not rdev.startswith("/dev/"):
            continue
        try:
            st = os.stat(rdev)
        except OSError:
            continue
        if not stat_mod.S_ISBLK(st.st_mode):
            continue
        rows.append((rdev, tgt))
    return rows


def _patrol_collect_disk_partitions() -> List[Tuple[str, str]]:
    """存在 /vol1、/vol2、/vol3 等数据卷挂载时只统计这些卷，避免 Docker 噪声；否则统计全部块设备挂载。

    合并 ``psutil.disk_partitions(all=True)`` 与 ``findmnt``，并解析 UUID=/LABEL=，减少漏盘。
    """
    out_ps: List[Tuple[str, str]] = []
    if psutil:
        try:
            for p in psutil.disk_partitions(all=True):
                dev = str(p.device or "").strip()
                mnt = str(p.mountpoint or "").strip()
                fst = str(p.fstype or "").lower()
                if not mnt or mnt == "/":
                    continue
                if fst in _PATROL_PSUTIL_NOISE_FSTYPES:
                    continue
                low = mnt.lower()
                if "docker" in low or "containerd" in low or "/kub" in low:
                    continue
                if mnt.startswith(("/proc/", "/sys/", "/run/credentials", "/snap/")):
                    continue
                if _patrol_mount_is_file_bind_noise(mnt):
                    continue
                rdev = _patrol_resolve_psutil_device(dev)
                if not rdev.startswith("/dev/"):
                    continue
                out_ps.append((rdev, mnt))
        except Exception:
            out_ps = []
    by_mount: Dict[str, Tuple[str, str]] = {}
    for d, m in out_ps:
        by_mount[m] = (d, m)
    for d, m in _patrol_findmnt_block_mounts():
        if m not in by_mount:
            by_mount[m] = (d, m)
    out = list(by_mount.values())
    vol = [(d, m) for d, m in out if _patrol_mount_is_data_volish(m)]
    return vol if vol else out


def _patrol_list_visible_whole_disks() -> List[str]:
    """列出系统可见物理整盘（sda / nvme0n1 等），用于按盘而非按卷统计。"""
    names: List[str] = []
    # lsblk TYPE=disk 优先（更贴近 smartctl 枚举）
    out = _run_cmd(["lsblk", "-dnro", "NAME,TYPE"], timeout=3.0)
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1].lower() != "disk":
            continue
        name = _normalize_block_name(parts[0])
        if name and _patrol_is_whole_disk_name(name):
            names.append(name)

    sys_block = Path("/sys/block")
    try:
        for p in sorted(sys_block.iterdir(), key=lambda x: x.name):
            name = _normalize_block_name(p.name)
            if name and _patrol_is_whole_disk_name(name):
                names.append(name)
    except Exception:
        pass
    return _dedupe_block_names(names)


def _disk_size_gb_for_disk(dev_base: str) -> str:
    dev = str(dev_base or "").strip()
    if not dev or not _valid_sysfs_block_token(dev):
        return "--"
    sys_size = Path(f"/sys/block/{dev}/size")
    try:
        sectors = int(sys_size.read_text(encoding="utf-8", errors="ignore").strip())
        if sectors > 0:
            return f"{sectors * 512 / (1024**3):.1f}"
    except Exception:
        pass
    out = _run_cmd(["lsblk", "-b", "-dnro", "SIZE", f"/dev/{dev}"], timeout=2.0)
    for line in (out or "").splitlines():
        raw = line.strip()
        if not raw or not raw.isdigit():
            continue
        size = int(raw)
        if size > 0:
            return f"{size / (1024**3):.1f}"
    return "--"


def _disk_total_gb_for_mount(mount: str) -> str:
    if not psutil:
        return "--"
    try:
        total = psutil.disk_usage(mount).total
        if total > 0:
            return f"{total / (1024**3):.1f}"
    except Exception:
        pass
    return "--"


def _patrol_mounts_to_probe_for_physical() -> List[str]:
    """按顺序探测挂载点，用于将整盘名（sda）反查到 /vol* 等数据卷。"""
    ordered: List[str] = []
    seen: Set[str] = set()
    for p in (
        *_patrol_visible_fn_vol_mount_roots(),
        "/volume1",
        "/volume2",
        "/volume3",
        "/volume4",
    ) + tuple(_PATROL_HOST_STORAGE_PATH_CANDIDATES):
        s = str(p).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        ordered.append(s)
    return ordered


def _patrol_best_mount_for_physical(dev_base: str) -> str:
    """根据块设备反查其数据卷挂载路径（典型 /vol1–/vol3），供剩余空间统计。"""
    want = str(dev_base or "").strip().lower()
    if not want:
        return ""
    for base in _patrol_mounts_to_probe_for_physical():
        if _patrol_mount_is_file_bind_noise(base):
            continue
        try:
            if not Path(base).exists():
                continue
        except OSError:
            continue
        src = _patrol_mount_source_for_path(base)
        if not src:
            continue
        rdev = src if str(src).startswith("/dev/") else _patrol_resolve_psutil_device(src)
        if not rdev.startswith("/dev/"):
            continue
        try:
            rdev = os.path.realpath(rdev)
        except OSError:
            pass
        phy = [x.lower() for x in _resolve_physical_disk_names(rdev)]
        if want in phy:
            return base
    return ""


def _patrol_df_space_gb_pair(path: str) -> Tuple[str, str]:
    """用 ``df`` 得到 ``(剩余GB, 总容量GB)`` 字符串；优先 GNU ``--output``，否则 ``-Pk``。"""
    p = str(path or "").strip()
    if not p:
        return "--", "--"
    out = _run_cmd(["df", "-B1", "--output=size,avail", "--", p], timeout=2.0)
    lines = [ln for ln in (out or "").splitlines() if ln.strip() and not ln.lstrip().lower().startswith("df:")]
    if len(lines) >= 2:
        parts = lines[-1].split()
        if len(parts) >= 2:
            try:
                sz_b = int(parts[0])
                av_b = int(parts[1])
                if sz_b > 0 and av_b >= 0:
                    return f"{av_b / (1024**3):.1f}", f"{sz_b / (1024**3):.1f}"
            except (TypeError, ValueError, IndexError):
                pass
    out2 = _run_cmd(["df", "-Pk", "--", p], timeout=2.0)
    lines2 = [ln for ln in (out2 or "").splitlines() if ln.strip() and not ln.lstrip().lower().startswith("df:")]
    if len(lines2) < 2:
        return "--", "--"
    parts = lines2[-1].split()
    if len(parts) < 4:
        return "--", "--"
    try:
        blocks_1k = int(parts[1])
        avail_1k = int(parts[3])
        if blocks_1k <= 0 or avail_1k < 0:
            return "--", "--"
        total_gb = f"{blocks_1k / (1024**2):.1f}"
        free_gb = f"{avail_1k / (1024**2):.1f}"
        return free_gb, total_gb
    except (TypeError, ValueError):
        return "--", "--"


def _patrol_df_free_gb_for_path(path: str) -> str:
    """df 解析可用空间（GB），与 ``_patrol_df_space_gb_pair`` 一致。"""
    f, _ = _patrol_df_space_gb_pair(path)
    return f


def _patrol_free_gb_str_for_mount(mnt: str) -> str:
    """挂载点剩余空间（GB）；优先 ``df``（与系统一致），再 psutil、statvfs。"""
    m = str(mnt or "").strip()
    if not m:
        return "--"
    try:
        if not Path(m).exists():
            return "--"
    except OSError:
        return "--"
    df_f = _patrol_df_free_gb_for_path(m)
    if df_f not in {"--", "—"}:
        return df_f
    if psutil:
        try:
            u = psutil.disk_usage(m)
            if u.total > 0 and u.free >= 0:
                return f"{u.free / (1024**3):.1f}"
        except Exception:
            pass
    try:
        stv = os.statvfs(m)
        fr = int(stv.f_frsize)
        bavail = int(stv.f_bavail) * fr
        bfree = int(stv.f_bfree) * fr
        use_b = bavail if bavail > 0 else bfree
        if use_b >= 0:
            return f"{use_b / (1024**3):.1f}"
    except OSError:
        pass
    return "--"


def _patrol_partition_dev_paths(dev_base: str) -> List[str]:
    """整盘下的分区块设备路径（如 /dev/sda1、/dev/nvme0n1p1），供 findmnt -S 反查挂载点。"""
    bd = (dev_base or "").strip()
    if not bd or not _valid_sysfs_block_token(bd):
        return []
    bpath = Path(f"/sys/block/{bd}")
    if not bpath.is_dir():
        try:
            core = Path(f"/dev/{bd}")
            return [str(core)] if core.exists() else []
        except OSError:
            return []
    out: List[str] = []
    try:
        for ch in sorted(bpath.iterdir()):
            if not ch.is_dir():
                continue
            name = ch.name
            if name == bd or not name.startswith(bd):
                continue
            devn = Path(f"/dev/{name}")
            try:
                if devn.exists():
                    out.append(str(devn))
            except OSError:
                continue
    except OSError:
        pass
    if not out:
        try:
            core = Path(f"/dev/{bd}")
            if core.exists():
                out.append(str(core))
        except OSError:
            pass
    return out


def _patrol_free_gb_via_findmnt_for_disk(dev_base: str) -> str:
    """对整盘各分区执行 ``findmnt -S``，在挂载点上再读剩余空间（不依赖 /vol 反查）。"""
    free_s, _ = _patrol_space_gb_via_findmnt_for_disk(dev_base)
    return free_s


def _patrol_space_gb_via_findmnt_for_disk(dev_base: str) -> Tuple[str, str]:
    """对整盘各分区 findmnt，返回 (剩余GB, 文件系统总容量GB)。"""
    best_free: Optional[float] = None
    best_total: Optional[float] = None
    for part in _patrol_partition_dev_paths(dev_base):
        out = _run_cmd(["findmnt", "-n", "-r", "-S", part, "-o", "TARGET"], timeout=2.5)
        if not out:
            continue
        for line in (out or "").splitlines():
            tgt = (line or "").strip()
            if not tgt:
                continue
            tgt = tgt.split()[0]
            if _patrol_mount_is_file_bind_noise(tgt):
                continue
            try:
                if not Path(tgt).exists():
                    continue
            except OSError:
                continue
            free_s, tot_s = _patrol_df_space_gb_pair(tgt)
            if free_s in {"--", "—"} or tot_s in {"--", "—"}:
                continue
            try:
                fv = float(free_s)
                tv = float(tot_s)
            except (TypeError, ValueError):
                continue
            if fv < 0 or tv <= 0:
                continue
            if best_free is None or fv > best_free:
                best_free = fv
                best_total = tv
    if best_free is not None and best_total is not None:
        return f"{best_free:.1f}", f"{best_total:.1f}"
    return "--", "--"


def _patrol_vol_bag_for_physical(
    physical: str,
    vol_space_by_physical: Optional[Dict[str, Dict[str, str]]] = None,
) -> Optional[Dict[str, str]]:
    """按物理整盘名取 /vol 空间；扫描表 miss 时反向遍历可见 /volN。"""
    pl = str(physical or "").strip().lower()
    if not pl:
        return None
    vol_space = vol_space_by_physical or {}
    hit = vol_space.get(pl)
    if hit:
        fg = str(hit.get("free_gb") or "").strip()
        tg = str(hit.get("total_gb") or "").strip()
        if fg and tg and fg not in {"--", "—"} and tg not in {"--", "—"}:
            return hit

    for vol in _patrol_visible_fn_vol_mount_roots():
        if _patrol_mount_is_file_bind_noise(vol):
            continue
        try:
            if not Path(vol).exists():
                continue
        except OSError:
            continue
        src = _patrol_mount_source_for_path(vol)
        if not src:
            continue
        rdev = src.strip() if str(src).startswith("/dev/") else _patrol_resolve_psutil_device(src)
        if not rdev.startswith("/dev/"):
            continue
        try:
            rdev_open = os.path.realpath(_patrol_block_dev_for_inspection(rdev))
        except OSError:
            rdev_open = _patrol_block_dev_for_inspection(rdev)
        names = {x.lower() for x in _resolve_physical_disk_names(rdev_open)}
        walked = _walk_lsblk_to_physical_base(rdev_open)
        if walked:
            names.add(walked.lower())
        if pl not in names:
            continue
        free_s, tot_s = _patrol_df_space_gb_pair(vol)
        if free_s in {"--", "—"} or tot_s in {"--", "—"}:
            if psutil:
                try:
                    u = psutil.disk_usage(vol)
                    if u.total > 0:
                        tot_s = f"{u.total / (1024**3):.1f}"
                        free_s = f"{u.free / (1024**3):.1f}"
                except Exception:
                    pass
        if free_s not in {"--", "—"} and tot_s not in {"--", "—"}:
            return {"free_gb": free_s, "total_gb": tot_s}
    return None


def _patrol_disk_space_gb_for_row(
    mount: str,
    physical: str,
    vol_space_by_physical: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[str, str]:
    """巡检单行 (剩余GB, 总容量GB)；优先 /vol 文件系统容量，再挂载点 df、findmnt。"""
    phy = str(physical or "").strip()
    m0 = str(mount or "").strip()

    bag = _patrol_vol_bag_for_physical(phy, vol_space_by_physical)
    if bag:
        return str(bag.get("free_gb") or "--"), str(bag.get("total_gb") or "--")

    if not m0 and phy:
        m0 = _patrol_best_mount_for_physical(phy)
    if m0 and not _patrol_mount_is_file_bind_noise(m0):
        free_s, tot_s = _patrol_df_space_gb_pair(m0)
        if free_s not in {"--", "—"} and tot_s not in {"--", "—"}:
            return free_s, tot_s

    if phy:
        fm_free, fm_tot = _patrol_space_gb_via_findmnt_for_disk(phy)
        if fm_free not in {"--", "—"} and fm_tot not in {"--", "—"}:
            return fm_free, fm_tot

        free_s = _patrol_disk_free_gb_for_row(m0, phy, vol_space_by_physical)
        hw_size = _disk_size_gb_for_disk(phy)
        if free_s not in {"--", "—"} and hw_size not in {"--", "—"}:
            return free_s, hw_size

    hw_size = _disk_size_gb_for_disk(phy) if phy else "--"
    return "--", hw_size if hw_size not in {"--", "—"} else "--"


def _patrol_scan_vol_space_by_physical() -> Dict[str, Dict[str, str]]:
    """正向扫描容器内可见的 /volN：每卷的 df/用量映射到底层整盘，含剩余与文件系统总容量。"""
    out: Dict[str, Dict[str, float]] = {}
    for vol in _patrol_visible_fn_vol_mount_roots():
        try:
            if not Path(vol).exists():
                continue
        except OSError:
            continue
        if _patrol_mount_is_file_bind_noise(vol):
            continue
        free_str, tot_str = _patrol_df_space_gb_pair(vol)
        if free_str in {"--", "—"} or tot_str in {"--", "—"}:
            if psutil:
                try:
                    u = psutil.disk_usage(vol)
                    if u.total > 0:
                        tot_str = f"{u.total / (1024**3):.1f}"
                        free_str = f"{u.free / (1024**3):.1f}"
                except Exception:
                    pass
        if free_str in {"--", "—"} or tot_str in {"--", "—"}:
            continue
        try:
            free_val = float(free_str)
            tot_val = float(tot_str)
        except (TypeError, ValueError):
            continue
        if free_val < 0 or tot_val <= 0:
            continue
        src = _patrol_mount_source_for_path(vol)
        if not src:
            continue
        rdev = src.strip() if str(src).startswith("/dev/") else _patrol_resolve_psutil_device(src)
        if not rdev.startswith("/dev/"):
            continue
        dev_open = _patrol_block_dev_for_inspection(rdev)
        try:
            dev_open = os.path.realpath(dev_open)
        except OSError:
            pass
        names = _resolve_physical_disk_names(dev_open)
        walked = _walk_lsblk_to_physical_base(dev_open)
        if walked:
            names = list(dict.fromkeys(list(names or []) + [walked]))
        if not names:
            try:
                base = os.path.basename(os.path.realpath(_patrol_block_dev_for_inspection(rdev)))
            except OSError:
                base = os.path.basename(rdev)
            nb = _normalize_block_name(base) or base
            if nb and _patrol_is_whole_disk_name(nb):
                names = [nb]
        sing = _resolve_physical_disk_name(dev_open)
        if sing and _patrol_is_whole_disk_name(sing):
            names = list(dict.fromkeys(list(names or []) + [sing]))
        for raw_phy in names or []:
            key = str(raw_phy or "").strip().lower()
            if not key:
                continue
            prev = out.get(key)
            if prev is None or free_val > prev["free"]:
                out[key] = {"free": free_val, "total": tot_val}
    return {
        k: {"free_gb": f"{v['free']:.1f}", "total_gb": f"{v['total']:.1f}"}
        for k, v in out.items()
    }


def _patrol_disk_free_gb_for_row(
    mount: str,
    physical: str,
    vol_space_by_physical: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    """巡检单行剩余空间：优先 /vol 正向映射（与总容量同源），再挂载点、反查卷、findmnt、lsblk。"""
    vol_space = vol_space_by_physical or {}
    m0 = str(mount or "").strip()
    phy = str(physical or "").strip()
    pl = phy.strip().lower()
    if pl:
        bag = vol_space.get(pl) or {}
        hit = bag.get("free_gb")
        if hit and str(hit).strip() and hit not in {"--", "—"}:
            return str(hit).strip()

    if m0 and not _patrol_mount_is_file_bind_noise(m0):
        got = _patrol_free_gb_str_for_mount(m0)
        if got not in {"--", "—"}:
            return got
    if phy:
        m_alt = _patrol_best_mount_for_physical(phy)
        if m_alt and m_alt != m0:
            got2 = _patrol_free_gb_str_for_mount(m_alt)
            if got2 not in {"--", "—"}:
                return got2
        fm = _patrol_free_gb_via_findmnt_for_disk(phy)
        if fm not in {"--", "—"}:
            return fm
        fb = _disk_free_gb_for_disk(phy)
        if fb not in {"--", "—"}:
            return fb
    if m0 and not _patrol_mount_is_file_bind_noise(m0):
        return _patrol_free_gb_str_for_mount(m0)
    return "--"


def _disk_free_gb_for_disk(dev_base: str) -> str:
    """从 lsblk 的文件系统字段读取整盘或其子设备的真实可用空间。"""
    dev = str(dev_base or "").strip()
    if not dev or not _valid_sysfs_block_token(dev):
        return "--"

    out = _run_cmd(
        ["lsblk", "-b", "-J", "-o", "NAME,TYPE,FSAVAIL,MOUNTPOINT,MOUNTPOINTS", f"/dev/{dev}"],
        timeout=3.0,
    )
    if not out:
        return _disk_free_gb_from_mounted_paths_for_disk(dev)
    try:
        obj = json.loads(out)
    except Exception:
        return _disk_free_gb_from_mounted_paths_for_disk(dev)

    def _mounts_for_node(node: Dict[str, Any]) -> List[str]:
        raw = node.get("mountpoints")
        mounts: List[str] = []
        if isinstance(raw, list):
            mounts.extend(str(x).strip() for x in raw if str(x or "").strip())
        elif isinstance(raw, str) and raw.strip():
            mounts.extend(x.strip() for x in raw.splitlines() if x.strip())
        raw_one = node.get("mountpoint")
        if isinstance(raw_one, str) and raw_one.strip():
            mounts.append(raw_one.strip())
        return mounts

    def _walk(node: Dict[str, Any]) -> List[int]:
        vals: List[int] = []
        mounts = _mounts_for_node(node)
        if mounts and not all(_patrol_mount_is_file_bind_noise(m) for m in mounts):
            raw = node.get("fsavail")
            try:
                if raw is None or raw is False or raw == "":
                    raise ValueError
                avail = int(raw)
                if avail >= 0:
                    vals.append(avail)
            except (TypeError, ValueError):
                pass
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    vals.extend(_walk(child))
        return vals

    values: List[int] = []
    for block in obj.get("blockdevices") or []:
        if isinstance(block, dict):
            values.extend(_walk(block))
    if not values:
        return _disk_free_gb_from_mounted_paths_for_disk(dev)
    return f"{max(values) / (1024**3):.1f}"


def _patrol_mount_source_for_path(path: str) -> str:
    p = str(path or "").strip()
    if not p:
        return ""
    out = _run_cmd(["findmnt", "-T", p, "-rno", "SOURCE"], timeout=2.0)
    if out:
        src = out.strip().splitlines()[0].strip()
        if src:
            return src
    out = _run_cmd(["df", "-P", p], timeout=2.0)
    lines = [x for x in (out or "").splitlines() if x.strip()]
    if len(lines) >= 2:
        return lines[-1].split()[0].strip()
    return ""


def _disk_free_gb_from_mounted_paths_for_disk(dev_base: str) -> str:
    dev = str(dev_base or "").strip()
    if not dev:
        return "--"
    candidates: List[Tuple[str, str]] = []

    if psutil:
        try:
            for p in psutil.disk_partitions(all=True):
                mnt = str(p.mountpoint or "").strip()
                src = str(p.device or "").strip()
                if not mnt or not src or _patrol_mount_is_file_bind_noise(mnt):
                    continue
                candidates.append((src, mnt))
        except Exception:
            pass

    for path in _PATROL_HOST_STORAGE_PATH_CANDIDATES:
        if _patrol_mount_is_file_bind_noise(path):
            continue
        try:
            if not Path(path).exists():
                continue
        except OSError:
            continue
        src = _patrol_mount_source_for_path(path)
        if src:
            candidates.append((src, path))

    best: Optional[float] = None
    for src, mount in candidates:
        rdev = _patrol_resolve_psutil_device(src) if not str(src).startswith("/dev/") else str(src).strip()
        if not rdev.startswith("/dev/"):
            continue
        if dev not in _resolve_physical_disk_names(rdev):
            continue
        try:
            free = psutil.disk_usage(mount).free / (1024**3) if psutil else 0.0
        except Exception:
            continue
        if free < 0:
            continue
        if best is None or free > best:
            best = free
    return f"{best:.1f}" if best is not None else "--"


def _patrol_disk_row_merge_key(dev_path: str, physical: str, display_dev: str, mount: str) -> str:
    """同一整盘（nvme0n1/sda）多挂载点合并；dm 设备单独成键；其余按 realpath 或 设备@挂载 区分。"""
    p = (physical or "").strip().rstrip("-")
    if p and _patrol_is_whole_disk_name(p):
        mnv = re.match(r"^(nvme\d+n\d+)", p, re.I)
        if mnv:
            return mnv.group(1).lower()
        return p
    if p and _valid_sysfs_block_token(p) and re.fullmatch(r"dm-\d+", p):
        return p
    try:
        rp = os.path.realpath(dev_path)
        if str(rp).startswith("/dev/"):
            return rp
    except Exception:
        pass
    d = (display_dev or "").strip().rstrip("-")
    return f"{d}@{mount}" if d else f"unknown@{mount}"


def _psutil_disk_temp_fallback(device_name: str) -> str:
    """从 psutil 传感器中按设备名提取温度（如 nvme0n1/sda/sdb）。

    与飞牛一致：优先 Composite / Sensor 1 / temp1，跳过 Sensor 2+。
    """
    if not psutil:
        return "--"
    dev = str(device_name or "").strip().lower()
    if not dev:
        return "--"
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        return "--"
    if not isinstance(temps, dict):
        return "--"

    def _to_temp(v: Any) -> str:
        try:
            fv = float(v)
            if fv > 200:
                fv = fv - 273.15
            if not _celsius_ok(fv):
                return "--"
            return _fmt_celsius(fv)
        except Exception:
            return "--"

    def _label_rank(label: str) -> int:
        low = (label or "").lower()
        if re.search(r"sensor\s*[2-9]", low) or re.search(r"temp\s*[2-9]", low):
            return 99
        if "composite" in low or low in {"", "temp1", "temp", "temperature"}:
            return 0
        if "sensor 1" in low or "sensor1" in low or "temp1" in low:
            return 1
        return 5

    # 1) 先按设备名匹配，再按 label 优先级
    candidates: List[Tuple[int, str]] = []
    for chip, entries in temps.items():
        chip_s = str(chip or "").lower()
        for e in entries or []:
            label = str(getattr(e, "label", "") or "")
            blob = f"{chip_s} {label.lower()}"
            if dev not in blob and not (dev.startswith("nvme") and "nvme" in chip_s):
                continue
            rank = _label_rank(label)
            if rank >= 99:
                continue
            t = _to_temp(getattr(e, "current", None))
            if t != "--":
                candidates.append((rank, t))
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    # 2) nvme/sata/drivetemp：取该 chip 下 rank 最低的一条（跳过 Sensor2）
    preferred_chips: List[str] = []
    if dev.startswith("nvme"):
        preferred_chips = ["nvme", "drivetemp", "sata"]
    else:
        preferred_chips = ["drivetemp", "sata", "nvme"]
    for pref in preferred_chips:
        chip_cands: List[Tuple[int, str]] = []
        for chip, entries in temps.items():
            chip_s = str(chip or "").lower()
            if pref not in chip_s:
                continue
            for e in entries or []:
                label = str(getattr(e, "label", "") or "")
                rank = _label_rank(label)
                if rank >= 99:
                    continue
                t = _to_temp(getattr(e, "current", None))
                if t != "--":
                    chip_cands.append((rank, t))
        if chip_cands:
            chip_cands.sort(key=lambda x: x[0])
            return chip_cands[0][1]
    return "--"


def _patrol_vol_row_health_temp(dev_open: str, physicals: List[str]) -> Tuple[str, str, str, str]:
    """存储空间 volN 行：聚合 SMART/sysfs；展示与飞牛界面一致的「正常」而非内部「健康」。"""
    hr_fail = "未读到健康状态（smartctl/nvme/sysfs 均未取得，可能是命令缺失、权限或设备映射限制）"
    tr_fail = "未读到温度（smartctl/nvme/sysfs 均未取得，可能是命令缺失、权限或设备映射限制）"
    phys = [p for p in (physicals or []) if (p or "").strip()]
    hs: List[str] = []
    if phys:
        for p in phys:
            h = _smart_health_for_patrol_partition(dev_open, p)
            if h not in {"--", "—"}:
                hs.append(h)
            for part in _patrol_partition_dev_paths(p):
                hp = _smart_health_for_block_path(part)
                if hp not in {"--", "—"}:
                    hs.append(hp)
    else:
        h0 = _smart_health_for_patrol_partition(dev_open, "")
        if h0 not in {"--", "—"}:
            hs.append(h0)
        try:
            hp0 = _smart_health_for_block_path(os.path.realpath(dev_open))
        except OSError:
            hp0 = _smart_health_for_block_path(dev_open)
        if hp0 not in {"--", "—"}:
            hs.append(hp0)

    for p in phys:
        if _patrol_sysfs_disk_running(p):
            hs.append("健康")

    if any(h == "异常" for h in hs):
        health = "异常"
    elif any(h == "健康" for h in hs):
        health = "正常"
    else:
        health = "--"

    temp = "--"
    if phys:
        for p in phys:
            t = _smart_temp_for_patrol_partition(dev_open, p)
            if t not in {"--", "—"}:
                temp = t
                break
        if temp in {"--", "—"}:
            temp = _psutil_disk_temp_fallback(str(phys[0]))
    else:
        temp = _smart_temp_for_patrol_partition(dev_open, "")
        if temp in {"--", "—"}:
            try:
                bn = os.path.basename(os.path.realpath(dev_open))
            except OSError:
                bn = os.path.basename(dev_open)
            temp = _psutil_disk_temp_fallback(bn)
    temp_reason = "" if temp not in {"--", "—"} else tr_fail

    health_reason = ""
    if health in {"--", "—"}:
        if any(_patrol_sysfs_disk_running(p) for p in phys) or temp not in {"--", "—"}:
            health = "正常"
            health_reason = ""
        else:
            health_reason = hr_fail
    return health, temp, health_reason, temp_reason


def _patrol_vol_mount_sort_key(mpath: str) -> int:
    m = re.search(r"/vol(\d+)$", str(mpath).strip(), re.I)
    return int(m.group(1)) if m else 0


def _patrol_discover_fn_vol_mount_root_paths() -> List[str]:
    """扫描容器根目录下所有 /volN（N 为任意正整数，不限于 vol1～vol6）。"""
    found: List[str] = []
    try:
        for entry in Path("/").iterdir():
            if not entry.is_dir():
                continue
            if re.fullmatch(r"vol\d+", entry.name, re.I):
                found.append(f"/{entry.name}")
    except OSError:
        pass
    return sorted(found, key=_patrol_vol_mount_sort_key)


def _patrol_visible_fn_vol_mount_roots() -> List[str]:
    """容器内可见且看起来像真实存储的 /volN（排除 Docker 空 bind 伪卷）。"""
    visible: List[str] = []
    for p in _patrol_discover_fn_vol_mount_root_paths():
        try:
            if Path(p).is_dir() and _patrol_fn_vol_looks_like_real_storage(p):
                visible.append(p)
        except OSError:
            continue
    return visible


def _patrol_has_any_fn_vol_mount_in_container() -> bool:
    return bool(_patrol_visible_fn_vol_mount_roots())


def _patrol_fn_compose_vol_mounts_ready() -> bool:
    """兼容旧名：任一 /volN 在容器内可见即视为已挂载存储卷。"""
    return _patrol_has_any_fn_vol_mount_in_container()


def _fnos_overview_for_patrol(cfg: Any) -> Dict[str, Dict[str, Any]]:
    """巡检用：拉一次飞牛监控接口的盘温/健康总览（未启用或不可用返回空）。

    数据源故障绝不能阻断巡检——任何异常都吞掉并降级到 smartctl 命令链。
    """
    if cfg is None or not getattr(cfg, "fnos_monitor_enabled", False):
        return {}
    from .fnos_ws import fetch_disk_overview

    try:
        overview, err = fetch_disk_overview(
            getattr(cfg, "fnos_monitor_url", ""),
            getattr(cfg, "fnos_monitor_user", ""),
            getattr(cfg, "fnos_monitor_password", ""),
        )
    except Exception as e:  # noqa: BLE001 — 数据源层已兜底，这里防意外类型
        overview, err = {}, str(e)
    if err:
        logging.getLogger(__name__).warning(
            "飞牛监控接口不可用，巡检盘温/健康回退 smartctl 命令链：%s", err
        )
        return {}
    return overview


def _collect_disk_items(cfg: Any = None) -> List[Dict[str, str]]:
    """按物理整盘（sda / nvme0n1…）生成巡检行，不再按 /volN 卷数分行。

    温度/健康优先取飞牛监控接口（启用且配置了凭据时；一次 WebSocket 会话
    覆盖全部盘，且能识别休眠盘——休眠盘不再做任何会唤醒硬盘的探测），
    取不到的盘回退对整盘路径的 smartctl/sysfs 探测；剩余空间仍尽量从
    关联的 /vol 挂载反查（有则填，无则 --）。
    """
    items: List[Dict[str, str]] = []
    vol_space_by_physical = _patrol_scan_vol_space_by_physical()
    whole_disks = _patrol_list_visible_whole_disks()
    fnos_overview = _fnos_overview_for_patrol(cfg)
    fnos_on = bool(fnos_overview) or bool(cfg is not None and getattr(cfg, "fnos_monitor_enabled", False))

    def _sort_disk_key(name: str) -> Tuple[int, str]:
        n = str(name or "").lower()
        if n.startswith("nvme"):
            return (0, n)
        if n.startswith("sd"):
            return (1, n)
        return (2, n)

    for physical in sorted(whole_disks, key=_sort_disk_key):
        info = fnos_overview.get(physical) or {}
        standby = bool(info.get("standby"))
        temp = str(info.get("temp") or "")
        passed = info.get("passed")
        health = {True: "健康", False: "异常"}.get(passed, "")
        if standby:
            # 休眠盘：跳过一切探测（smartctl/sysfs 查询会唤醒硬盘），按休眠展示
            temp, health = "", "休眠"
        else:
            if not temp:
                t = _smart_temp_for_disk(physical)
                if t in {"--", "—"}:
                    t = _psutil_disk_temp_fallback(physical)
                if t not in {"--", "—"}:
                    temp = t
            if not health:
                h = _smart_health_for_disk(physical)
                if h in {"--", "—"}:
                    if _patrol_sysfs_disk_running(physical) or temp:
                        h = "健康"
                health = "" if h in {"--", "—"} else h
        # smartctl -n standby 探明休眠态：温度/健康无意义，按休眠展示，
        # 且不再做任何会唤醒硬盘的补充探测
        if temp == "休眠" or health == "休眠":
            standby = True
            temp, health = "", "休眠"
        if health == "健康":
            health = "正常"
        health_reason = ""
        if not health:
            health = "--"
            health_reason = "未读到健康状态（飞牛监控接口与 smartctl/nvme/sysfs 均未取得，可能是命令缺失、权限或设备映射限制）" if fnos_on else (
                "未读到健康状态（smartctl/nvme/sysfs 均未取得，可能是命令缺失、权限或设备映射限制）"
            )
        temp_reason = ""
        if not temp:
            temp = "--"
            temp_reason = "未读到温度（飞牛监控接口与 smartctl/nvme/sysfs 均未取得，可能是命令缺失、权限或设备映射限制）" if fnos_on else (
                "未读到温度（smartctl/nvme/sysfs 均未取得，可能是命令缺失、权限或设备映射限制）"
            )
        if standby:
            temp_reason = "硬盘休眠，已跳过探测（避免唤醒）"
            health_reason = ""
        m_pick = _patrol_best_mount_for_physical(physical)
        free_s, size_s = _patrol_disk_space_gb_for_row(m_pick, physical, vol_space_by_physical)
        items.append(
            {
                "name": "",
                "device": physical,
                "mount_point": m_pick,
                "free_gb": free_s if free_s else "--",
                "size_gb": size_s if size_s else "--",
                "temp_c": temp,
                "status": health,
                "temp_reason": temp_reason,
                "status_reason": health_reason,
            }
        )

    for idx, row in enumerate(items, start=1):
        row["name"] = f"硬盘{idx}"
    return items


