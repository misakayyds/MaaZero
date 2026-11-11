"""Pure-Python ARM64 static ELF generator and validator for test harnesses."""

import struct
from pathlib import Path
from typing import Optional


def build_arm64_elf_bytes(
    is_static: bool = True,
    is_64bit: bool = True,
    is_little_endian: bool = True,
    machine: int = 0x00B7,  # EM_AARCH64 (183)
    elf_type: int = 2,      # ET_EXEC
    interp_path: str = "/lib/ld-linux-aarch64.so.1",
    bad_magic: bool = False,
) -> bytes:
    """Build synthetic ELF binary bytes for testing packaging and binary validation.

    If is_static is True, emits only a PT_LOAD segment (no PT_INTERP).
    If is_static is False, emits a PT_LOAD segment and a PT_INTERP segment.
    """
    endian = "<" if is_little_endian else ">"

    magic = b"\x00ELF" if bad_magic else b"\x7fELF"
    ei_class = 2 if is_64bit else 1
    ei_data = 1 if is_little_endian else 2
    ei_version = 1
    ei_osabi = 0
    ei_abiversion = 0
    e_ident = magic + bytes([ei_class, ei_data, ei_version, ei_osabi, ei_abiversion]) + (b"\x00" * 7)

    if not is_64bit:
        # 32-bit ELF Header (52 bytes)
        e_entry = 0x10000
        e_phoff = 52
        e_shoff = 0
        e_flags = 0
        e_ehsize = 52
        e_phentsize = 32
        e_phnum = 1
        e_shentsize = 40
        e_shnum = 0
        e_shstrndx = 0
        header = e_ident + struct.pack(
            f"{endian}HHIIIIIHHHHHH",
            elf_type,
            machine,
            1,  # e_version
            e_entry,
            e_phoff,
            e_shoff,
            e_flags,
            e_ehsize,
            e_phentsize,
            e_phnum,
            e_shentsize,
            e_shnum,
            e_shstrndx,
        )
        # 1 PT_LOAD Program Header (32 bytes)
        ph = struct.pack(f"{endian}IIIIIIII", 1, 0, e_entry, e_entry, 100, 100, 5, 0x1000)
        return header + ph + (b"\x00" * 16)

    # 64-bit ELF Header (64 bytes)
    e_entry = 0x400000
    e_phoff = 64
    e_shoff = 0
    e_flags = 0
    e_ehsize = 64
    e_phentsize = 56
    e_phnum = 1 if is_static else 2
    e_shentsize = 64
    e_shnum = 0
    e_shstrndx = 0

    header = e_ident + struct.pack(
        f"{endian}HHIQQQIHHHHHH",
        elf_type,
        machine,
        1,  # e_version
        e_entry,
        e_phoff,
        e_shoff,
        e_flags,
        e_ehsize,
        e_phentsize,
        e_phnum,
        e_shentsize,
        e_shnum,
        e_shstrndx,
    )

    # Program Header 1: PT_LOAD (type 1)
    ph_load = struct.pack(
        f"{endian}IIQQQQQQ",
        1,       # p_type = PT_LOAD
        5,       # p_flags = PF_R | PF_X
        0,       # p_offset
        e_entry, # p_vaddr
        e_entry, # p_paddr
        512,     # p_filesz
        512,     # p_memsz
        0x10000, # p_align
    )

    if is_static:
        return header + ph_load + (b"\x90" * 64)

    # Program Header 2: PT_INTERP (type 3)
    interp_bytes = interp_path.encode("utf-8") + b"\x00"
    interp_offset = e_ehsize + (e_phentsize * e_phnum)
    ph_interp = struct.pack(
        f"{endian}IIQQQQQQ",
        3,                    # p_type = PT_INTERP
        4,                    # p_flags = PF_R
        interp_offset,        # p_offset
        e_entry + interp_offset,
        e_entry + interp_offset,
        len(interp_bytes),    # p_filesz
        len(interp_bytes),    # p_memsz
        1,                    # p_align
    )

    return header + ph_load + ph_interp + interp_bytes + (b"\x90" * 32)


def create_arm64_elf(
    path: str | Path,
    is_static: bool = True,
    is_64bit: bool = True,
    is_little_endian: bool = True,
    machine: int = 0x00B7,
    elf_type: int = 2,
    interp_path: str = "/lib/ld-linux-aarch64.so.1",
    bad_magic: bool = False,
) -> Path:
    """Create an ARM64 ELF file on disk with the requested characteristics."""
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    raw = build_arm64_elf_bytes(
        is_static=is_static,
        is_64bit=is_64bit,
        is_little_endian=is_little_endian,
        machine=machine,
        elf_type=elf_type,
        interp_path=interp_path,
        bad_magic=bad_magic,
    )
    target_path.write_bytes(raw)
    return target_path
