"""Bounded file access on the grid storage and a narrow, allowlisted frontend command tool."""

from __future__ import annotations

import base64
import os
import re
import shlex
from pathlib import Path
from typing import Any

from .errors import PolicyError, RemoteCommandError
from .gate import Gate, q
from .textutil import human_size, looks_binary, truncate_lines

SHELL_META = re.compile(r"[;&|<>`$(){}\\\n\r*?\[\]!~]")
# Subcommands that change state, refused even for allowlisted programs.
MODULE_READONLY = {"avail", "av", "list", "show", "display", "spider", "whatis", "keyword", "search", "help"}


class Files:
    def __init__(self, gate: Gate):
        self.gate = gate
        self.cfg = gate.cfg

    def list_dir(self, path: str, pattern: str | None) -> dict[str, Any]:
        p = self.gate.resolve(path)
        limit = self.cfg.limits.max_list_entries
        name_filter = f" -name {q(pattern)}" if pattern else ""
        out = self.gate.run_ok(f"""
if [ ! -d {q(p)} ]; then echo "__NOTDIR__"; exit 0; fi
echo "__COUNT__ $(find {q(p)} -mindepth 1 -maxdepth 1{name_filter} 2>/dev/null | wc -l)"
find {q(p)} -mindepth 1 -maxdepth 1{name_filter} -printf '%y\\t%s\\t%TY-%Tm-%Td %TH:%TM\\t%f\\n' 2>/dev/null \
  | sort -t$'\\t' -k4 | head -n {limit}
""", "listing directory")
        if "__NOTDIR__" in out:
            raise PolicyError(f"{p} is not a directory")
        lines = out.splitlines()
        total = int(lines[0].split()[1]) if lines and lines[0].startswith("__COUNT__") else None
        entries = []
        for line in lines[1:]:
            t, size, mtime, name = (line.split("\t") + ["", "", "", ""])[:4]
            entries.append({"name": name + ("/" if t == "d" else ""), "type": {"d": "dir", "f": "file", "l": "link"}.get(t, t),
                            "size": human_size(int(size)) if t == "f" and size.isdigit() else None, "mtime": mtime})
        res = {"path": p, "total_entries": total, "entries": entries}
        if total and total > len(entries):
            res["note"] = f"showing {len(entries)} of {total}; pass a pattern (e.g. '*.vcf.gz') to narrow"
        return res

    def read_file(self, path: str, max_bytes: int | None, offset: int, tail_lines: int | None) -> dict[str, Any]:
        p = self.gate.resolve(path)
        cap = self.cfg.limits.max_read_bytes
        n = max(1, min(max_bytes or 16 * 1024, cap))
        gz = p.endswith(".gz")
        if tail_lines:
            tail_lines = min(tail_lines, self.cfg.limits.max_output_lines)
            reader = (f"zcat -- {q(p)} | tail -n {tail_lines}" if gz else f"tail -n {tail_lines} -- {q(p)}")
            reader += f" | tail -c {n}"
        elif gz:
            reader = f"zcat -- {q(p)} 2>/dev/null | tail -c +{offset + 1} | head -c {n}"
        else:
            reader = f"tail -c +{offset + 1} -- {q(p)} | head -c {n}"
        out = self.gate.run_ok(f"""
if [ ! -f {q(p)} ]; then echo "__NOFILE__"; exit 0; fi
echo "__SIZE__ $(stat -c %s {q(p)})"
{reader} | base64 -w0
""", "reading file")
        if "__NOFILE__" in out:
            raise PolicyError(f"{p} does not exist or is not a regular file")
        first, _, b64 = out.partition("\n")
        size = int(first.split()[1])
        data = base64.b64decode(b64.strip() or b"")
        if looks_binary(data):
            return {"path": p, "size": human_size(size), "binary": True,
                    "note": "binary file; use a summary tool in a job or interactive session "
                            "(e.g. samtools flagstat / quickcheck, bcftools stats, seqkit stats)"}
        text = data.decode(errors="replace")
        res: dict[str, Any] = {"path": p, "size_bytes": size, "size": human_size(size), "content": text}
        if gz:
            res["decompressed"] = True
        if tail_lines:
            res["mode"] = f"last {tail_lines} lines"
        else:
            end = offset + len(data)
            res["range"] = [offset, end]
            if (not gz and end < size) or (gz and len(data) == n):
                res["truncated"] = True
                res["next_offset"] = end
                res["note"] = ("partial read. Prefer summaries (wc -l, grep -c, samtools/bcftools stats) over "
                               "paging through large files.")
        return res

    def write_file(self, path: str, content: str, overwrite: bool, executable: bool) -> dict[str, Any]:
        data = content.encode()
        if len(data) > self.cfg.limits.max_write_bytes:
            raise PolicyError(f"content is {human_size(len(data))}; the cap is "
                              f"{human_size(self.cfg.limits.max_write_bytes)}. Use upload_file for larger files.")
        p = self.gate.resolve(path, write=True)
        b64 = base64.b64encode(data).decode()
        out = self.gate.run_ok(f"""
set -e
if [ -e {q(p)} ] && [ {1 if overwrite else 0} = 0 ]; then echo "__EXISTS__"; exit 0; fi
if [ -d {q(p)} ]; then echo "__ISDIR__"; exit 0; fi
mkdir -p "$(dirname {q(p)})"
tmp={q(p)}.mcp-tmp.$$
printf %s {q(b64)} | base64 -d > "$tmp"
{"chmod +x \"$tmp\"" if executable else ":"}
mv -f "$tmp" {q(p)}
echo "__OK__ $(stat -c %s {q(p)})"
""", "writing file")
        if "__EXISTS__" in out:
            raise PolicyError(f"{p} already exists; pass overwrite=true to replace it")
        if "__ISDIR__" in out:
            raise PolicyError(f"{p} is a directory")
        return {"path": p, "bytes_written": len(data), "executable": executable}

    def make_dir(self, path: str) -> dict[str, Any]:
        p = self.gate.resolve(path, write=True)
        self.gate.run_ok(f"mkdir -p -- {q(p)}", "creating directory")
        return {"path": p, "created": True}

    # ------------------------------------------------------------- transfers
    def _local(self, path: str, must_exist: bool) -> Path:
        lp = Path(path).expanduser()
        if not lp.is_absolute():
            lp = Path(os.getcwd()) / lp
        lp = lp.resolve()
        roots = [Path(r) for r in self.cfg.workspace.local_roots]
        if not any(lp == r or r in lp.parents for r in roots):
            raise PolicyError(f"local path {lp} is outside the allowed local roots {[str(r) for r in roots]}")
        if must_exist and not lp.exists():
            raise PolicyError(f"local path {lp} does not exist")
        return lp

    def upload(self, local_path: str, remote_path: str, overwrite: bool) -> dict[str, Any]:
        lp = self._local(local_path, must_exist=True)
        if lp.is_dir():
            raise PolicyError("directory upload is not supported; upload files (or a tar archive) one at a time")
        size = lp.stat().st_size
        if size > self.cfg.limits.max_transfer_bytes:
            raise PolicyError(f"{lp} is {human_size(size)}; transfer cap is {human_size(self.cfg.limits.max_transfer_bytes)}")
        if remote_path.endswith("/"):  # explicit directory, created if missing
            remote_path = remote_path + lp.name
        rp = self.gate.resolve(remote_path, write=True)
        out = self.gate.run_ok(f"""
if [ -d {q(rp)} ]; then echo "__DIR__"; exit 0; fi
if [ -e {q(rp)} ]; then echo "__EXISTS__"; exit 0; fi
mkdir -p "$(dirname {q(rp)})"; echo ok
""", "preparing upload")
        if "__DIR__" in out:
            rp = f"{rp}/{lp.name}"
        elif "__EXISTS__" in out and not overwrite:
            raise PolicyError(f"{rp} exists; pass overwrite=true to replace it")
        self.gate.transport.scp(str(lp), self.gate.transport.remote_spec(rp))
        return {"local": str(lp), "remote": rp, "bytes": size, "size": human_size(size)}

    def download(self, remote_path: str, local_path: str, overwrite: bool) -> dict[str, Any]:
        rp = self.gate.resolve(remote_path)
        out = self.gate.run_ok(f"[ -f {q(rp)} ] && stat -c %s {q(rp)} || echo __NOFILE__", "stat")
        if "__NOFILE__" in out:
            raise PolicyError(f"{rp} is not a regular file")
        size = int(out.strip())
        if size > self.cfg.limits.max_transfer_bytes:
            raise PolicyError(f"{rp} is {human_size(size)}; transfer cap is {human_size(self.cfg.limits.max_transfer_bytes)}")
        lp = self._local(local_path, must_exist=False)
        if lp.is_dir():
            lp = lp / Path(rp).name
        if lp.exists() and not overwrite:
            raise PolicyError(f"{lp} exists locally; pass overwrite=true to replace it")
        lp.parent.mkdir(parents=True, exist_ok=True)
        self.gate.transport.scp(self.gate.transport.remote_spec(rp), str(lp))
        return {"remote": rp, "local": str(lp), "bytes": size, "size": human_size(size)}

    # ------------------------------------------------------- frontend_exec
    def frontend_exec(self, command: str) -> dict[str, Any]:
        fx = self.cfg.frontend_exec
        if not fx.enabled:
            raise PolicyError("frontend_exec is disabled by configuration; use an interactive session")
        if SHELL_META.search(command):
            raise PolicyError("shell syntax (pipes, redirects, ;, $, globs, ...) is not allowed on the frontend. "
                              "Run real work in an interactive session or a batch job.")
        try:
            argv = shlex.split(command)
        except ValueError as e:
            raise PolicyError(f"cannot parse command: {e}") from e
        if not argv:
            raise PolicyError("empty command")
        prog = argv[0]
        if prog not in fx.allowed_commands:
            raise PolicyError(f"{prog!r} is not allowed on the shared frontend (login node). Allowed: "
                              f"{fx.allowed_commands}. Anything heavier goes to interactive_run or submit_job.")
        if prog == "module" and (len(argv) < 2 or argv[1] not in MODULE_READONLY):
            raise PolicyError(f"only read-only module subcommands are allowed here: {sorted(MODULE_READONLY)}. "
                              "Load modules inside the interactive session or the job script.")
        cmd = " ".join(q(a) for a in argv)
        r = self.gate.run(f"nice -n 10 timeout {fx.timeout} {cmd} 2>&1"
                          if prog != "module" else f"timeout {fx.timeout} bash -lc {q(cmd + ' 2>&1')}",
                          timeout=fx.timeout + 15)
        text = r.stdout
        text, clipped = truncate_lines(text.rstrip("\n"), head=60, tail=self.cfg.limits.max_output_lines // 5)
        if r.rc not in (0, 1) and not text:
            raise RemoteCommandError(f"command failed (exit {r.rc}): {r.stderr.strip()[-500:]}")
        return {"command": cmd, "exit_code": r.rc, "output": text, "truncated": clipped}
