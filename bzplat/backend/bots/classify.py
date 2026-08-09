"""上传文件分类；平台唯一可执行目标是 Linux x86_64 ELF64（小端）。"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from ..store.schema import (
    SUPPORTED_BINARY_ARCH,
    SUPPORTED_BINARY_ERROR,
    SUPPORTED_BINARY_FORMAT,
    SUPPORTED_BINARY_OS,
)


class BinaryRejectError(ValueError):
    """文件不满足平台唯一的可执行格式契约。"""


@dataclass(frozen=True)
class BinaryInfo:
    # ``pe``/``macho``/``unknown`` 仅用于给历史文件明确诊断，绝不表示可运行。
    format: str
    os: str
    arch: str
    runnable: bool
    reject_reason: str = ""


def require_supported_binary(info: BinaryInfo) -> BinaryInfo:
    """返回唯一受支持的目标；对伪造/历史元数据一律 fail closed。"""
    if (
        info.runnable
        and info.format == SUPPORTED_BINARY_FORMAT
        and info.os == SUPPORTED_BINARY_OS
        and info.arch == SUPPORTED_BINARY_ARCH
    ):
        return info
    raise BinaryRejectError(info.reject_reason or SUPPORTED_BINARY_ERROR)


def _unsupported(detail: str) -> str:
    return f"{SUPPORTED_BINARY_ERROR}；{detail}"


def classify_binary(data: bytes) -> BinaryInfo:
    if len(data) < 4:
        return BinaryInfo(
            "unknown", "unknown", "unknown", False, _unsupported("文件过小")
        )

    # ELF
    if data[:4] == b"\x7fELF":
        return _classify_elf(data)
    # PE (MZ)
    if data[:2] == b"MZ":
        return _classify_pe(data)
    # Mach-O
    macho_magics = {
        0xFEEDFACE, 0xCEFAEDFE, 0xFEEDFACF, 0xCFFAEDFE, 0xCAFEBABE, 0xBEBAFECA,
    }
    if len(data) >= 4:
        magic = struct.unpack(">I", data[:4])[0]
        magic_le = struct.unpack("<I", data[:4])[0]
        if magic in macho_magics or magic_le in macho_magics:
            return BinaryInfo(
                "macho", "macos", _macho_arch(data), False,
                _unsupported("macOS Mach-O 不受支持"),
            )
    return BinaryInfo(
        "unknown", "unknown", "unknown", False,
        _unsupported("无法识别的可执行格式"),
    )


def _classify_elf(data: bytes) -> BinaryInfo:
    if len(data) < 64:
        return BinaryInfo(
            "elf", "linux", "unknown", False, _unsupported("ELF64 头不完整")
        )
    ei_class = data[4]  # 1=32, 2=64
    ei_data = data[5]  # 1=LE, 2=BE
    ei_version = data[6]
    ei_osabi = data[7]
    if ei_class != 2:
        return BinaryInfo(
            "elf", "linux", "i386" if ei_class == 1 else "unknown", False,
            _unsupported("必须是 ELF64，ELF32 不受支持"),
        )
    if ei_data != 1:
        return BinaryInfo(
            "elf", "linux", "unknown", False,
            _unsupported("必须是小端 ELF"),
        )
    if ei_version != 1:
        return BinaryInfo(
            "elf", "linux", "unknown", False,
            _unsupported("ELF 版本无效"),
        )
    # Linux 工具链通常写 System V(0) 或 GNU/Linux(3) OSABI。其他 ABI 即使同为
    # ELF64/EM_X86_64 也不能假定可在 Linux 容器执行。
    if ei_osabi not in (0, 3):
        return BinaryInfo(
            "elf", "unknown", "unknown", False,
            _unsupported(f"ELF OSABI {ei_osabi} 不受支持"),
        )
    # e_machine at offset 18 (EI_NIDENT=16 + 2)
    e_type, e_machine = struct.unpack_from("<HH", data, 16)
    arch = "unknown"
    if e_machine == 0x3E:
        arch = "amd64"
    elif e_machine == 0xB7:
        arch = "arm64"
    elif e_machine == 0x03:
        arch = "i386"
    elif e_machine == 0x28:
        arch = "arm"
    if e_machine != 0x3E:
        return BinaryInfo(
            "elf", "linux", arch, False,
            _unsupported(f"ELF e_machine={e_machine:#x} 不是 EM_X86_64"),
        )
    # ET_EXEC=2；ET_DYN=3 覆盖现代 PIE 可执行文件。目标/核心文件均不可执行。
    if e_type not in (2, 3):
        return BinaryInfo(
            "elf", "linux", arch, False,
            _unsupported(f"ELF e_type={e_type} 不是可执行文件"),
        )
    return BinaryInfo(
        SUPPORTED_BINARY_FORMAT,
        SUPPORTED_BINARY_OS,
        SUPPORTED_BINARY_ARCH,
        True,
    )


def _classify_pe(data: bytes) -> BinaryInfo:
    if len(data) < 0x40:
        return BinaryInfo(
            "pe", "windows", "unknown", False,
            _unsupported("Windows PE 不受支持（PE 头过短）"),
        )
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 6 > len(data) or data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        return BinaryInfo(
            "pe", "windows", "unknown", False,
            _unsupported("Windows PE 不受支持（签名无效）"),
        )
    machine = struct.unpack_from("<H", data, e_lfanew + 4)[0]
    arch = {0x14C: "i386", 0x8664: "amd64", 0xAA64: "arm64"}.get(machine, "unknown")
    return BinaryInfo(
        "pe", "windows", arch, False,
        _unsupported("Windows PE 不受支持"),
    )


def _macho_arch(data: bytes) -> str:
    # Best-effort; not executed on Linux host
    if len(data) < 8:
        return "unknown"
    magic = struct.unpack(">I", data[:4])[0]
    if magic in (0xCFFAEDFE, 0xFEEDFACF):
        cpu = struct.unpack("<I", data[4:8])[0]
    elif magic in (0xCEFAEDFE, 0xFEEDFACE):
        cpu = struct.unpack("<I", data[4:8])[0]
    else:
        return "unknown"
    if cpu in (0x01000007, 7):
        return "amd64"
    if cpu in (0x0100000C, 12):
        return "arm64"
    return "unknown"
