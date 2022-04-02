import argparse
import subprocess
import shlex
import re
import struct
from pathlib import Path, PurePosixPath

parser = argparse.ArgumentParser()

parser.add_argument("-r", "--rbase", type=Path, required=True)
parser.add_argument("-i", "--idb", type=Path, required=True)
parser.add_argument("-m", "--mach", action="append")
parser.add_argument("-p", "--progress", action="store_true")
parser.add_argument("-M", "--force-mode", type=lambda v: int(v, 8), default=0o600)
parser.add_argument("-F", "--force-dir-mode", type=lambda v: int(v, 8), default=0o700)
parser.add_argument("-v", "--verbose", action="count", default=0)

args = parser.parse_args()

args.rbase = args.rbase.resolve()

active_machs = {}


def parse_mach(mach):
    mach = mach.split("=", 1)
    if len(mach) == 1:
        return "CPUBOARD", mach[0]
    else:
        return mach[0], mach[1]


for mach in args.mach or ():
    mach, value = parse_mach(mach)
    if mach in active_machs:
        print(f"mach `{mach}` already set")
        exit(1)
    active_machs[mach] = value

ARG_RE = re.compile(r"""([^ ]+) *""")
ATTR_RE = re.compile(r"""([a-z]+)\(((?:[^")]|"(?:[^"]|\\.)*")*)\) *""")

archives = {}
archive_indices = {}


def open_archive(name):
    try:
        return archives[name]
    except KeyError:
        pass
    archives[name] = archive = open(args.idb.parent / name, "rb")

    while archive.read(1) not in (b"", b"\x00"):
        pass

    return archive


def index_archive(name):
    data = open_archive(name)


lines = list(args.idb.open())
total = len(lines)
if args.progress:
    from tqdm import tqdm

    lines = tqdm(lines)

for i, line in enumerate(lines, 1):
    prefix = f"[{i}/{total}]"
    line = line.rstrip("\n")
    pos = 0

    line_args = []
    attr = {}

    def int_attr(name, default=None):
        (attr_value,) = attr.get(name, (default,))
        return int(attr_value)

    while pos < len(line):
        if match := ATTR_RE.match(line, pos):
            pos = match.end()
            if match[1] in attr:
                print(f"{prefix} warning: duplicate attribute: {match[1]}")
            attr[match[1]] = shlex.split(match[2])
        elif match := ARG_RE.match(line, pos):
            pos = match.end()
            line_args.append(match[1])

    command, *line_args = line_args

    if command in "dfl":
        mode, user, group, raw_path, *line_args = line_args
        prefix = f"{prefix} {raw_path}:"
        mode = int(mode, 8)
        path = PurePosixPath(raw_path)

        skip = False
        if machs := attr.get("mach"):
            possible_machs = {}
            inverted_machs = {}
            for mach in machs:
                mach, value = parse_mach(mach)
                if mach.endswith("!"):
                    inverted_machs.setdefault(mach, []).append(value)
                else:
                    possible_machs.setdefault(mach, []).append(value)

            for mach, values in inverted_machs.items():
                if mach in active_machs and active_machs[mach] in values:
                    skip = True
                    if args.verbose > 1:
                        print(
                            f"{prefix} skipped due to mach"
                            f" `{mach}={active_machs[mach]}` in `{values}"
                        )
                    continue

            if not skip:
                for mach, values in possible_machs.items():
                    if mach not in active_machs:
                        print(f"{prefix} warning: unknown mach `{mach}` `{values}`")
                        skip = True
                        break
                    if active_machs[mach] not in values:
                        skip = True
                        if args.verbose > 1:
                            print(
                                f"{prefix} skipped due to mach"
                                f" `{mach}={active_machs[mach]}` not in `{values}"
                            )
                        break

        if not skip:
            parts = []
            for part in path.parts:
                if part == "/":
                    continue
                if part == "..":
                    if not parts.pop():
                        print(f"{prefix} path starts with `..`")
                else:
                    parts.append(part)

            path = PurePosixPath("")
            for part in parts:
                path /= part

            out_path = args.rbase / path

            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
            except FileExistsError:
                resolved = out_path.parent.resolve()
                if resolved.is_relative_to(args.rbase):
                    resolved.mkdir(parents=True, exist_ok=True)
                else:
                    raise

        if command == "d" and not skip:
            if "delhist" in line_args:
                continue
            if args.verbose:
                print(f"{prefix} creating directory `{path}`")
            out_path.mkdir(mode=mode | args.force_dir_mode, exist_ok=True)
        elif command == "f":
            if args.verbose and not skip:
                print(f"{prefix} extracting `{path}`")
            src_path, *line_args = line_args

            archive = None
            for i, arg in enumerate(line_args):
                if arg.count(".") >= 2:
                    archive = arg
                    del line_args[i]
                    break

            if archive is None:
                print(f"{prefix} error: no archive file specified")
                continue

            archive, archive_tag = archive.rsplit(".", 1)

            off = int_attr("off", -1)
            size = int_attr("size")
            cmpsize = int_attr("cmpsize", 0)

            if cmpsize:
                extsize = cmpsize
                compressed = True
            else:
                extsize = size
                compressed = False

            try:
                data = open_archive(archive)
            except FileNotFoundError:
                print(f"{prefix} error: archive {archive} not found")
                continue

            if off >= 0:
                data.seek(off)

            (pathlen,) = struct.unpack(">h", data.read(2))
            if pathlen != len(raw_path):
                print(f"{prefix} error: wrong pathlen {pathlen} in archive")
                continue

            pathdata = data.read(pathlen)

            if pathdata != raw_path.encode():
                print(f"{prefix} error: wrong path {pathdata!r} in archive")
                continue

            content = data.read(extsize)

            if len(content) != extsize:
                print(f"{prefix} error: unexpected end of archive")
                continue

            orig_content = content

            if not skip:
                if compressed:
                    content = subprocess.check_output("uncompress", input=content)

                    if len(content) != size:
                        print(f"{prefix} error: unexpected size of decompressed data")
                        continue

                stored_sum = int_attr("sum", -1)

                if stored_sum >= 0:

                    data_sum = int(
                        subprocess.check_output("sum", input=content)
                        .decode("ascii")
                        .split()[0]
                    )

                    if data_sum != stored_sum:
                        print(
                            f"{prefix} warning: checksum mismatch:"
                            f" got {data_sum} expected {stored_sum}"
                        )
                        print(subprocess.check_output("sum", input=orig_content))
                        print(attr)

                out_path.write_bytes(content)
                out_path.chmod(mode | args.force_mode)

        elif command == "l" and not skip:
            if "noshare" in line_args:
                continue
            (symval,) = attr["symval"]
            symval = PurePosixPath(symval)
            if symval.is_absolute():
                symval = args.rbase / symval.relative_to("/")
            if args.verbose:
                print(f"{prefix} creating symlink `{path}` -> `{symval}`")
            try:
                out_path.symlink_to(symval)
            except FileExistsError:
                if out_path.resolve() != (out_path.parent / symval).resolve():
                    raise

    else:
        print(f"{prefix} warning: unknown command")
