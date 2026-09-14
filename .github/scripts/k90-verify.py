#!/usr/bin/env python3
"""Fail-closed validation for the K90 Google GKI + official KernelSU build."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import zipfile

ROOT = Path.cwd()
COMMON = ROOT / "kernel/common"
EVIDENCE = ROOT / "evidence"
DEFCONFIG = COMMON / "arch/arm64/configs/gki_defconfig"
NAME = "K90-6.6.118-android15-KernelSU-built-in-4k-AnyKernel3.zip"

def require(condition, message):
    if not condition:
        raise SystemExit("VALIDATION FAILED: " + message)

def run(*args):
    return subprocess.check_output(args, text=True).strip()

def config(path):
    return dict(re.findall(r"^(CONFIG_\w+)=(.*)$", path.read_text(), re.M))

def check_config(path):
    c = config(path)
    for key in ("KSU", "ARM64", "ARM64_4K_PAGES", "IKCONFIG", "IKCONFIG_PROC",
                "KPROBES", "EXT4_FS", "SECURITY_SELINUX", "MODVERSIONS"):
        require(c.get("CONFIG_" + key) == "y", key + " must be y in " + str(path))
    for key in ("ARM64_16K_PAGES", "ARM64_64K_PAGES"):
        require(c.get("CONFIG_" + key) not in ("y", "m"), key + " enabled")
    require(not any(k.startswith("CONFIG_KSU_SUSFS") and v in ("y", "m")
                    for k, v in c.items()), "SUSFS must be absent")
    print("Verified:", path, "Page Size: 4K; CONFIG_KSU=y; SELinux enabled")
    return c

def find_config():
    base = ROOT / "kernel/bazel-bin/common/kernel_aarch64_config"
    found = list(base.rglob(".config"))
    require(len(found) == 1, "expected exactly one generated 4K config: " + str(found))
    return found[0]

def baseline():
    m = (COMMON / "Makefile").read_text()
    for key, value in (("VERSION", "6"), ("PATCHLEVEL", "6"), ("SUBLEVEL", "118")):
        require(re.search(r"^" + key + r"\s*=\s*" + value + r"\s*$", m, re.M),
                key + " must be " + value)
    build_config = (COMMON / "build.config.common").read_text()
    require(re.search(r"^BRANCH=android15-6\.6$", build_config, re.M), "Android 15 branch not verified")
    require(re.search(r"^KMI_GENERATION=8$", build_config, re.M), "KMI generation must be 8")
    (EVIDENCE / "build.config.common").write_text(build_config)
    print("Baseline verified: common-android15-6.6-2026-01, 6.6.118, arm64")
    print(build_config)

def configure():
    baseline()
    require(os.environ["root_flavor"] == "KernelSU", "wrong root flavor")
    ksu = ROOT / "kernel/KernelSU"
    require(run("git", "-C", str(ksu), "remote", "get-url", "origin") ==
            "https://github.com/tiann/KernelSU.git", "wrong KernelSU upstream")
    require(run("git", "-C", str(ksu), "rev-parse", "HEAD") ==
            os.environ["KSU_COMMIT"], "KernelSU commit mismatch")
    require("obj-$(CONFIG_KSU) += kernelsu.o" in (ksu / "kernel/Kbuild").read_text(),
            "KernelSU built-in Kbuild integration missing")
    # These are root prerequisites, page-size choice and embedded config evidence only.
    subprocess.run([str(COMMON / "scripts/config"), "--file", str(DEFCONFIG),
                    "-e", "KSU", "-e", "ARM64_4K_PAGES",
                    "-d", "ARM64_16K_PAGES", "-d", "ARM64_64K_PAGES",
                    "-e", "KPROBES", "-e", "IKCONFIG", "-e", "IKCONFIG_PROC"], check=True)
    # The pinned main-kernel-build-2024 Kleaf uses build.config.gki's
    # POST_DEFCONFIG_CMDS, not the newer check_defconfig Starlark API.
    path = COMMON / "build.config.gki"
    src = path.read_text()
    (EVIDENCE / "build.config.gki.original").write_text(src)
    require(src.count("check_defconfig") == 1, "unexpected GKI defconfig check")
    print("Original GKI config:", src)
    path.write_text(src.replace("check_defconfig", ""))
    # Only the equality check is removed, matching the repository's existing
    # build-kernel action. Actual final config validation remains mandatory.
    shutil.copyfile(DEFCONFIG, EVIDENCE / "requested-gki_defconfig")
    (EVIDENCE / "kernel-source-changes.patch").write_text(
        run("git", "-C", str(COMMON), "diff", "--"))
    (EVIDENCE / "kernelsu-commit.txt").write_text(os.environ["KSU_COMMIT"] + "\n")

def prebuild():
    path = find_config()
    check_config(path)
    shutil.copyfile(path, EVIDENCE / "prebuild.config")

def package():
    prebuild()
    output = ROOT / "kernel/bazel-bin/common/kernel_aarch64"
    image = output / "Image"
    require(image.is_file(), "missing Image")
    data = image.read_bytes()
    require(data[56:60] == b"ARM\x64", "not an arm64 Linux Image")
    flags = struct.unpack_from("<Q", data, 24)[0]
    require(((flags >> 1) & 3) == 1, "Image header does not encode 4K pages")
    extracted = subprocess.check_output([str(COMMON / "scripts/extract-ikconfig"), str(image)])
    (EVIDENCE / "final.config").write_bytes(extracted)
    check_config(EVIDENCE / "final.config")
    require(config(EVIDENCE / "prebuild.config") == config(EVIDENCE / "final.config"),
            "prebuild and Image-embedded configs differ")
    banners = re.findall(rb"Linux version ([^\x00\n]+)", data)
    require(banners, "missing Linux banner")
    banner = banners[0].decode(errors="replace")
    require(re.match(r"6\.6\.118-android15-8(?:-| )", banner),
            "unexpected kernel release/KMI: " + banner)
    system_map = output / "System.map"
    require(system_map.is_file(), "missing linked System.map")
    symbols = [line for line in system_map.read_text().splitlines()
               if re.search(r"\bksu_\w+", line)]
    require(len(symbols) >= 10, "KernelSU not linked into vmlinux")
    require(b"KernelSU" in data or b"kernelsu" in data or b"ksu_" in data,
            "no KernelSU evidence in Image")
    require(not list(output.rglob("kernelsu.ko")), "unexpected KernelSU LKM")
    (EVIDENCE / "kernelsu-linked-symbols.txt").write_text("\n".join(symbols) + "\n")
    staging = ROOT / "package"
    staging.mkdir()
    ak = ROOT / "AnyKernel3"
    for directory in ("META-INF", "tools"):
        shutil.copytree(ak / directory, staging / directory)
    shutil.copyfile(image, staging / "Image")
    # split_boot/flash_boot keeps the existing ramdisk; no ramdisk repack.
    # NO_MAGISK_CHECK also prevents automatic kernel/DTB Magisk patching.
    installer = """### K90 official KernelSU built-in: kernel-only AnyKernel3
properties() { '
kernel.string=K90-KernelSU 6.6.118 android15 4K built-in
do.devicecheck=1
do.modules=0
do.systemless=0
do.cleanup=1
do.cleanuponabort=0
device.name1=annibale
supported.versions=
supported.patchlevels=
supported.vendorpatchlevels=
'; }
BLOCK=boot;
IS_SLOT_DEVICE=1;
SLOT_SELECT=active;
RAMDISK_COMPRESSION=auto;
PATCH_VBMETA_FLAG=0;
NO_MAGISK_CHECK=1;
. tools/ak3-core.sh;
case "$(uname -r)" in
  6.6.*-android15-8-*) ;;
  *) abort "Requires Android 15 6.6 KMI generation 8";;
esac;
split_boot;
flash_boot;
"""
    (staging / "anykernel.sh").write_text(installer)
    archive = ROOT / "delivery" / NAME
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(staging))
    with zipfile.ZipFile(archive) as z:
        require(z.testzip() is None, "ZIP CRC error")
        names = z.namelist()
        for required in ("Image", "anykernel.sh", "tools/ak3-core.sh",
                         "META-INF/com/google/android/update-binary"):
            require(required in names, "missing ZIP entry " + required)
        require(not any(n.endswith(".ko") for n in names), "unexpected kernel module")
        require(z.read("Image") == data, "packaged Image mismatch")
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    report = {
        "commit": os.environ["GITHUB_SHA"],
        "run_url": "https://github.com/" + os.environ["GITHUB_REPOSITORY"] +
                   "/actions/runs/" + os.environ["GITHUB_RUN_ID"],
        "kernel": "6.6.118", "banner": banner,
        "baseline": "common-android15-6.6-2026-01", "kmi": "android15-6.6-8",
        "page_size": "4K", "image_header_page_size_verified": True,
        "root": "tiann/KernelSU", "root_commit": os.environ["KSU_COMMIT"],
        "built_in": True, "CONFIG_KSU": "y", "susfs": False,
        "anykernel3_commit": os.environ["AK3_COMMIT"],
        "artifact": NAME, "sha256": sha,
        "device_boot_tested": False,
    }
    (ROOT / "delivery" / (NAME + ".sha256")).write_text(sha + "  " + NAME + "\n")
    (EVIDENCE / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    (ROOT / "delivery/verification.json").write_text(json.dumps(report, indent=2) + "\n")
    summary = ("Page Size: 4K\n\nCONFIG_KSU=y (built-in), official tiann/KernelSU\n\n"
               "SUSFS: absent\n\nKernel: " + banner + "\n\nSHA256: " + sha +
               "\n\nBuild and package validation passed; device boot not tested.\n\n"
               "READY TO FLASH: " + NAME + "\n")
    (EVIDENCE / "summary.md").write_text(summary)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
        f.write(summary)
    print(json.dumps(report, indent=2))
    print(summary)

EVIDENCE.mkdir(exist_ok=True)
{"baseline": baseline, "configure": configure, "prebuild": prebuild, "package": package}[sys.argv[1]]()
