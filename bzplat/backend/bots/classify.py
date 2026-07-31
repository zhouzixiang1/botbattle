"""二进制魔数分类：ELF / PE / Mach-O + 架构。"""
from __future__ import annotations

import struct
from dataclasses import dataclass


class BinaryRejectError(ValueError):
    """不可在本平台执行（如 Mach-O）。"""


@dataclass(frozen=True)
class BinaryInfo:
    format: str  # elf | pe | macho | unknown
    os: str  # linux | windows | macos | unknown
    arch: str  # amd64 | arm64 | i386 | unknown
    runnable: bool
    reject_reason: str = ""


def classify_binary(data: bytes) -> BinaryInfo:
    if len(data) < 4:
        return BinaryInfo("unknown", "unknown", "unknown", False, "文件过小")

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
                "macOS Mach-O 无法在 Linux 服务器沙箱执行，请交叉编译为 Linux ELF 或 Windows PE",
            )
    return BinaryInfo("unknown", "unknown", "unknown", False, "无法识别的可执行格式")


def _classify_elf(data: bytes) -> BinaryInfo:
    if len(data) < 20:
        return BinaryInfo("elf", "linux", "unknown", False, "ELF 头不完整")
    ei_class = data[4]  # 1=32, 2=64
    ei_data = data[5]  # 1=LE, 2=BE
    # e_machine at offset 18 (EI_NIDENT=16 + 2)
    e_machine = struct.unpack("<H" if ei_data == 1 else ">H", data[18:20])[0]
    arch = "unknown"
    if e_machine == 0x3E:
        arch = "amd64"
    elif e_machine == 0xB7:
        arch = "arm64"
    elif e_machine == 0x03:
        arch = "i386"
    elif e_machine == 0x28:
        arch = "arm"
    runnable = arch in ("amd64", "arm64", "i386")
    return BinaryInfo(
        "elf", "linux", arch, runnable,
        "" if runnable else f"暂不支持的 ELF 架构: {e_machine:#x}",
    )


def _classify_pe(data: bytes) -> BinaryInfo:
    if len(data) < 0x40:
        return BinaryInfo("pe", "windows", "unknown", False, "PE 头过短")
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 6 > len(data) or data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        return BinaryInfo("pe", "windows", "unknown", False, "无效 PE 签名")
    machine = struct.unpack_from("<H", data, e_lfanew + 4)[0]
    arch = {0x14C: "i386", 0x8664: "amd64", 0xAA64: "arm64"}.get(machine, "unknown")
    runnable = arch in ("i386", "amd64")
    return BinaryInfo(
        "pe", "windows", arch, runnable,
        "" if runnable else f"暂不支持的 PE 架构: {machine:#x}（需要 Wine 容器）",
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
